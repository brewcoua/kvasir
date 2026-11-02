"""Driving a run of the question ledger.

Code decides how much to search and when to stop; the model only decides what to search for, what
a page says, and how to write it up. Every model call sees one small input, so a 16k context is
enough. Calls are sequential: a local server answers one at a time, so fanning out buys nothing.
"""

from __future__ import annotations

import datetime
import time
from typing import Any, TypeVar

from kvasir.config import Settings
from kvasir.methods.base import Method
from kvasir.methods.ledger import loop
from kvasir.methods.ledger.brief import candidates, clarifications
from kvasir.methods.ledger.compare import assess, fill, rank
from kvasir.methods.ledger.frontier import Frontier
from kvasir.methods.ledger.loop import Judge
from kvasir.methods.ledger.questions import (
    Question,
    add_questions,
    as_results,
    notes,
    open_questions,
)
from kvasir.methods.ledger.repair import repair
from kvasir.methods.ledger.signatures import Brief, Decompose, NamedQuestion, Outline
from kvasir.research import verify, write
from kvasir.research.calls import predict
from kvasir.research.claims import Claim
from kvasir.research.corroborate import cross_check
from kvasir.research.render import SUMMARY, matrix, render
from kvasir.research.text import names
from kvasir.runners import build_checker, build_research_models, build_research_tools
from kvasir.runs.progress import ASSESS, BACKGROUND, BRIEF, RESEARCH, VERIFY, WRITE, ProgressStream
from kvasir.schemas.requests import Input, LedgerOptions
from kvasir.schemas.results import Criterion, ResearchClaim, ResearchResult, Row

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
    settings: Settings, input: Input, options: LedgerOptions, stream: ProgressStream
) -> ResearchResult:
    """Research a question to a cited report, blocking for minutes. Never call this on the loop."""
    started = time.monotonic()
    models = build_research_models(
        settings, options.model_fast, options.model_strong, options.model_reason
    )
    checker = build_checker(settings)
    if checker is not None and _setting(options.ground, True):
        models["ground"] = checker
    if checker is not None and options.ground_intake:
        models["ground_intake"] = checker
    encoder, searxng = build_research_tools(settings)
    budget = _setting(options.max_searches, settings.research.max_searches)
    per_query = _setting(options.results_per_query, settings.research.results_per_query)
    attempts = _setting(options.max_attempts, settings.research.max_attempts)
    follow_ups = _setting(options.follow_ups, settings.research.follow_ups)
    max_depth = _setting(options.max_depth, settings.research.max_depth)

    # The model's sense of "now" is its training cutoff, so "best" would mean best then.
    today = datetime.date.today().isoformat()

    stream.publish(BRIEF, "writing the research brief")
    framed = predict(
        models["brief"],
        Brief,
        today=today,
        question=input.question,
        clarifications=clarifications(input.clarifications),
    )
    brief: str = framed.brief
    output = options.output or framed.output
    seen: set[str] = set()

    # Named in the question, they are final; the options override even those. The
    # brief lists options from memory as readily as from the question, so only those the question
    # really names count as named.
    asked = f"{input.question}\n{clarifications(input.clarifications)}"
    named = [name for name in framed.criteria if names(name, asked)]
    criteria = [Criterion(name=name) for name in options.criteria or named]
    entities = list(options.entities or [e for e in framed.entities if names(e, asked)])
    if output != "report":
        criteria, entities = candidates(
            models,
            searxng,
            today,
            brief,
            criteria,
            entities,
            _setting(options.max_criteria, settings.research.max_criteria),
            _setting(options.max_entities, settings.research.max_entities),
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
    add_questions(encoder, ledger, first or [NamedQuestion(title="", question=input.question)])

    def check(question: Question) -> None:
        asked = loop.check(models["check"], encoder, brief, claims, question)
        if follow_ups and question.status == "answered" and question.depth < max_depth:
            untitled = [NamedQuestion(title="", question=q) for q in asked[:FOLLOW_UPS]]
            add_questions(encoder, ledger, untitled, question)

    searched = 0

    def research(
        questions: list[Question], budget: int, judge: Judge, frontier: Frontier | None = None
    ) -> None:
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
            frontier,
        )

    # Only the first pass: filling cells and repairing sentences search for set questions.
    research(
        ledger,
        budget,
        check,
        Frontier(encoder, brief, claims) if options.frontier == "worth" else None,
    )

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
    if rows:
        sections.insert(0, (TABLE_TITLES[output], matrix(rows, criteria)))
    stream.publish(WRITE, "summing up the report", len(titles), len(titles))
    summary = write.summary(models["tldr"], brief, sections)
    stream.publish(WRITE, "assembled the report", len(titles), len(titles))
    if left := open_questions(ledger):
        sections.append(("Open questions", left))

    by_id = {claim.id: claim for claim in claims}
    # The summary is checked with the sections, so the checker is loaded once. Repair leaves it be.
    checked, removed = verify.report(
        models["verify"], [(SUMMARY, summary), *sections], by_id, stream, models.get("ground")
    )
    (_, summary), *sections = checked

    def judge(question: Question) -> None:
        loop.check(models["check"], encoder, brief, claims, question)

    sections, removed = repair(
        models, encoder, brief, titles, claims, sections, removed, research, judge, stream
    )

    cross_check(models, encoder, claims, stream, options.corroborate, options.contradict)

    by_id = {claim.id: claim for claim in claims}
    report, citations = render(sections, by_id, summary)
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
    """The option's value, or the configured default when the run leaves it out."""
    return default if value is None else value


def _stages(options: LedgerOptions) -> list[str]:
    # Background and assess happen only for a ranking or a table, which the brief may choose.
    if options.output == "report":
        return [BRIEF, RESEARCH, WRITE, VERIFY]
    return [BRIEF, BACKGROUND, RESEARCH, ASSESS, WRITE, VERIFY]


def _defaults(settings: Settings) -> dict[str, Any]:
    return {
        "max_searches": settings.research.max_searches,
        "results_per_query": settings.research.results_per_query,
        "max_attempts": settings.research.max_attempts,
        "follow_ups": settings.research.follow_ups,
        "max_depth": settings.research.max_depth,
        "max_entities": settings.research.max_entities,
        "max_criteria": settings.research.max_criteria,
        "ground": settings.model.checker is not None,
        "model_fast": settings.model.fast,
        "model_strong": settings.model.strong,
        "model_reason": settings.model.reason or settings.model.strong,
    }


LEDGER = Method(
    name="ledger",
    Options=LedgerOptions,
    clarifies=True,
    stages=_stages,
    defaults=_defaults,
    run=run_research,
)
