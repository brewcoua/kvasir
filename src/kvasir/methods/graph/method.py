"""Driving a run of the graph method.

The brief is split into first questions, as in the ledger. Exploration then searches the
question worth most, takes in its claims, and adds the questions they raise as its children, until
the budget is spent or every branch is closed. The report has one section per branch that found
something, written from the claims its questions rest on. Always a report: the graph has no
ranking or table.
"""

from __future__ import annotations

import datetime
import time
from typing import Any

from kvasir.config import Settings
from kvasir.methods.base import Method
from kvasir.methods.graph.explore import explore
from kvasir.methods.graph.graph import branches
from kvasir.methods.ledger.brief import clarifications
from kvasir.methods.ledger.frontier import Frontier
from kvasir.methods.ledger.method import FIRST_QUESTIONS, NO_EVIDENCE
from kvasir.methods.ledger.questions import Question, add_questions, as_results, open_questions
from kvasir.methods.ledger.signatures import Brief, Decompose, NamedQuestion
from kvasir.research import verify, write
from kvasir.research.calls import predict
from kvasir.research.claims import Claim
from kvasir.research.corroborate import cross_check
from kvasir.research.render import render
from kvasir.runners import build_checker, build_research_models, build_research_tools
from kvasir.runs.progress import BRIEF, RESEARCH, VERIFY, WRITE, ProgressStream
from kvasir.runs.run import record_questions
from kvasir.schemas.requests import GraphOptions, Input
from kvasir.schemas.results import ResearchClaim, ResearchResult


def run_graph(
    settings: Settings, input: Input, options: GraphOptions, stream: ProgressStream
) -> ResearchResult:
    """Research a question to a cited report, blocking for minutes. Never call this on the loop."""
    started = time.monotonic()
    models = build_research_models(settings, options.model_fast, options.model_strong)
    checker = build_checker(settings)
    if checker is not None and (options.ground is None or options.ground):
        models["ground"] = checker
    encoder, searxng = build_research_tools(settings)

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

    stream.publish(RESEARCH, "splitting the brief into questions")
    questions: list[Question] = []
    first = predict(models["decompose"], Decompose, brief=brief).questions[:FIRST_QUESTIONS]
    add_questions(encoder, questions, first or [NamedQuestion(title="", question=input.question)])
    record_questions(as_results(questions))
    claims: list[Claim] = []
    explore(
        models,
        encoder,
        searxng,
        today,
        brief,
        questions,
        claims,
        Frontier(encoder, brief, claims),
        options.max_searches or settings.research_max_searches,
        options.results_per_query or settings.research_results_per_query,
        options.max_attempts or settings.research_max_attempts,
        options.max_depth or settings.research_max_depth,
        options.branch_searches,
        stream,
    )

    def result(report: str, **fields: Any) -> ResearchResult:
        return ResearchResult(
            report=report,
            assumptions=list(framed.assumptions),
            claims=[ResearchClaim(**vars(claim)) for claim in claims],
            questions=as_results(questions),
            duration_seconds=round(time.monotonic() - started, 1),
            **fields,
        )

    if not claims:
        stream.publish(WRITE, "no evidence found, so nothing to write")
        return result(NO_EVIDENCE, citations=[])

    # A branch is a cluster: its first question titles the section, and the claims its questions
    # rest on are always given to it.
    found = [
        (branch[0].text, {i for q in branch for i in q.claims})
        for branch in branches(questions).values()
    ]
    found = [(title, ids) for title, ids in found if ids]
    titles = [title for title, _ in found]
    sections: list[tuple[str, str]] = []
    for done, (title, ids) in enumerate(found):
        stream.publish(WRITE, f"writing {title}", done, len(found))
        text = write.section(models["write"], encoder, brief, titles, title, sections, claims, ids)
        sections.append((title, text))
    stream.publish(WRITE, "assembled the report", len(found), len(found))
    if left := open_questions(questions):
        sections.append(("Open questions", left))

    by_id = {claim.id: claim for claim in claims}
    sections, removed = verify.report(
        models["verify"], sections, by_id, stream, models.get("ground")
    )
    cross_check(models, encoder, claims, stream, options.corroborate, options.contradict)
    report, citations = render(sections, by_id)
    return result(report, citations=citations, removed=removed)


def _defaults(settings: Settings) -> dict[str, Any]:
    return {
        "max_searches": settings.research_max_searches,
        "results_per_query": settings.research_results_per_query,
        "max_attempts": settings.research_max_attempts,
        "max_depth": settings.research_max_depth,
        "ground": settings.model_checker is not None,
        "model_fast": settings.model_fast,
        "model_strong": settings.model_strong,
    }


GRAPH = Method(
    name="graph",
    Options=GraphOptions,
    clarifies=True,
    stages=lambda options: [BRIEF, RESEARCH, WRITE, VERIFY],
    defaults=_defaults,
    run=run_graph,
)
