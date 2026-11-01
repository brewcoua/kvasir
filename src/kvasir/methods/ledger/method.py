"""Driving a run of the question ledger.

Code decides how much to search and when to stop; the model only decides what to search for, what
a page says, and how to write it up. Every model call sees one small input, so a 16k context is
enough. Calls are sequential: a local server answers one at a time, so fanning out buys nothing.
"""

from __future__ import annotations

import datetime
import time
from typing import TypeVar

from kvasir.config import Settings
from kvasir.methods.ledger import loop
from kvasir.methods.ledger.brief import candidates, clarifications
from kvasir.methods.ledger.compare import assess, fill, rank
from kvasir.methods.ledger.loop import Judge
from kvasir.methods.ledger.questions import (
    Question,
    add_questions,
    as_results,
    notes,
    open_questions,
)
from kvasir.methods.ledger.repair import repair
from kvasir.methods.ledger.signatures import Brief, Decompose, Outline
from kvasir.models import Criterion, ResearchClaim, ResearchRequest, ResearchResult, Row
from kvasir.progress import BACKGROUND, BRIEF, RESEARCH, WRITE, ProgressStream
from kvasir.research import verify, write
from kvasir.research.calls import predict
from kvasir.research.claims import Claim
from kvasir.research.render import matrix, render
from kvasir.research.text import names
from kvasir.runners import build_research_models, build_research_tools

# Questions the first split of the brief adds at most.
FIRST_QUESTIONS = 6
# Follow-ups one answered question adds at most.
FOLLOW_UPS = 2

SHAPES = {
    "report": "The first section gives the key findings. Each later section covers one part of "
    "the answer.",
    "ranking": "A ranking table comes first and is already built. Start with how the options were "
    "compared, then one section per option, best first.",
    "table": "A comparison table comes first and is already built. Write a short summary of what "
    "it shows, then at most two sections on what it leaves open.",
}
TABLE_TITLES = {"ranking": "Ranking", "table": "Comparison"}

NO_EVIDENCE = (
    "## No findings\n\nNo page read during the research supported a claim about this question. "
    "Try rephrasing it, or answering the clarifying questions."
)

T = TypeVar("T")


def run_research(
    settings: Settings, request: ResearchRequest, stream: ProgressStream
) -> ResearchResult:
    """Research a question to a cited report, blocking for minutes. Never call this on the loop."""
    started = time.monotonic()
    models = build_research_models(settings, request.model_fast, request.model_strong)
    encoder, searxng = build_research_tools(settings)
    budget = _setting(request.max_searches, settings.research_max_searches)
    per_query = _setting(request.results_per_query, settings.research_results_per_query)
    attempts = _setting(request.max_attempts, settings.research_max_attempts)
    follow_ups = _setting(request.follow_ups, settings.research_follow_ups)
    max_depth = _setting(request.max_depth, settings.research_max_depth)

    # The model's sense of "now" is its training cutoff, so "best" would mean best then.
    today = datetime.date.today().isoformat()

    stream.publish(BRIEF, "writing the research brief")
    framed = predict(
        models["brief"],
        Brief,
        today=today,
        question=request.question,
        clarifications=clarifications(request.clarifications),
    )
    brief: str = framed.brief
    output = request.output or framed.output
    seen: set[str] = set()

    # Named in the question, they are final; the request's own fields override even those. The
    # brief lists options from memory as readily as from the question, so only those the question
    # really names count as named.
    asked = f"{request.question}\n{clarifications(request.clarifications)}"
    named = [name for name in framed.criteria if names(name, asked)]
    criteria = [Criterion(name=name) for name in request.criteria or named]
    entities = list(request.entities or [e for e in framed.entities if names(e, asked)])
    if output != "report":
        criteria, entities = candidates(
            models,
            searxng,
            today,
            brief,
            criteria,
            entities,
            _setting(request.max_criteria, settings.research_max_criteria),
            _setting(request.max_entities, settings.research_max_entities),
            per_query,
            stream,
        )
        if not (criteria and entities):
            stream.publish(BACKGROUND, "found nothing to compare, so writing a report instead")
            output = "report"
        else:
            # Every later step reads the brief, so naming the options here steers the searches
            # and the intake towards them.
            brief += (
                f"\nOptions compared: {', '.join(entities)}"
                f"\nCriteria: {', '.join(c.name for c in criteria)}"
            )

    claims: list[Claim] = []
    stream.publish(RESEARCH, "splitting the brief into questions")
    ledger: list[Question] = []
    first = predict(models["decompose"], Decompose, brief=brief).questions[:FIRST_QUESTIONS]
    add_questions(encoder, ledger, first or [request.question])

    def check(question: Question) -> None:
        asked = loop.check(models["check"], encoder, brief, claims, question)
        if follow_ups and question.status == "answered" and question.depth < max_depth:
            add_questions(encoder, ledger, asked[:FOLLOW_UPS], question)

    searched = 0

    def research(questions: list[Question], budget: int, judge: Judge) -> None:
        nonlocal searched
        searched += loop.research(
            models,
            encoder,
            searxng,
            today,
            brief,
            questions,
            claims,
            seen,
            budget,
            per_query,
            attempts,
            judge,
            searched,
            stream,
        )

    research(ledger, budget, check)

    if not claims:
        # Writing sections from no evidence only produces paragraphs saying there is none.
        stream.publish(WRITE, "no evidence found, so nothing to write")
        return ResearchResult(
            report=NO_EVIDENCE,
            assumptions=list(framed.assumptions),
            citations=[],
            claims=[],
            questions=as_results(ledger),
            output=output,
            duration_seconds=round(time.monotonic() - started, 1),
        )

    rows: list[Row] = []
    if output != "report":
        rows = assess(models, encoder, brief, claims, entities, criteria, stream)
        fill(models["assess"], encoder, brief, claims, ledger, rows, research, stream)
        if output == "ranking":
            rows = rank(models["judge"], brief, criteria, rows, stream)
            brief += f"\nOverall ranking, best first: {', '.join(row.entity for row in rows)}"

    stream.publish(WRITE, "outlining the report")
    titles: list[str] = predict(
        models["outline"], Outline, brief=brief, shape=SHAPES[output], notes=notes(ledger)
    ).sections
    sections: list[tuple[str, str]] = []
    for done, title in enumerate(titles):
        stream.publish(WRITE, f"writing {title}", done, len(titles))
        sections.append(
            (title, write.section(models["write"], encoder, brief, titles, title, sections, claims))
        )
    stream.publish(WRITE, "assembled the report", len(titles), len(titles))
    if rows:
        sections.insert(0, (TABLE_TITLES[output], matrix(rows, criteria)))
    if left := open_questions(ledger):
        sections.append(("Open questions", left))

    by_id = {claim.id: claim for claim in claims}
    sections, removed = verify.report(models["verify"], sections, by_id, stream)

    def judge(question: Question) -> None:
        loop.check(models["check"], encoder, brief, claims, question)

    sections, removed = repair(
        models, encoder, brief, titles, claims, sections, removed, research, judge, stream
    )

    by_id = {claim.id: claim for claim in claims}
    report, citations = render(sections, by_id)
    return ResearchResult(
        report=report,
        assumptions=list(framed.assumptions),
        citations=citations,
        claims=[ResearchClaim(**vars(claim)) for claim in claims],
        questions=as_results(ledger),
        removed=removed,
        output=output,
        criteria=criteria if rows else [],
        matrix=rows,
        duration_seconds=round(time.monotonic() - started, 1),
    )


def _setting(value: T | None, default: T) -> T:
    """The request's value, or the configured default when the request leaves it out."""
    return default if value is None else value
