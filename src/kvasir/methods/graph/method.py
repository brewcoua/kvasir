"""Driving a run of the graph method.

The brief is split into first questions, as in the ledger. Exploration then searches the
question proof numbers pick, takes in its claims, and adds the questions they raise as its
children, until the research time is spent or no question can still be settled. The report has
sections an outline call groups the questions into after the research, each written from the
claims its questions rest on. Always a report: the graph has no ranking or table.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any

from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.config import Settings
from kvasir.methods.base import Method
from kvasir.methods.graph.explore import explore
from kvasir.methods.graph.graph import branches
from kvasir.methods.graph.signatures import Outline
from kvasir.methods.ledger.brief import clarifications
from kvasir.methods.ledger.frontier import Frontier
from kvasir.methods.ledger.method import FIRST_QUESTIONS, NO_EVIDENCE
from kvasir.methods.ledger.questions import UNSETTLED, Question, add_questions, as_results
from kvasir.methods.ledger.signatures import Brief, Decompose, NamedQuestion
from kvasir.research import verify, write
from kvasir.research.calls import predict
from kvasir.research.claims import Claim, nearest
from kvasir.research.corroborate import cross_check
from kvasir.research.render import SUMMARY, render
from kvasir.runners import build_checker, build_research_models, build_research_tools
from kvasir.runs.progress import BRIEF, RESEARCH, VERIFY, WRITE, ProgressStream
from kvasir.runs.run import record_questions
from kvasir.schemas.requests import GraphOptions, Input
from kvasir.schemas.results import ResearchClaim, ResearchResult
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel

# Levels of raised questions below the first questions, unless the run sets it. Deeper than the
# ledger's: the time budget, not the depth, is what bounds the graph.
MAX_DEPTH = 4
# Sections a report has at most, as STORM's outlines do; more read as a list of findings.
MAX_SECTIONS = 7
# Questions the report lists as open, at most.
OPEN_QUESTIONS = 10

logger = logging.getLogger(__name__)


def run_graph(
    settings: Settings, input: Input, options: GraphOptions, stream: ProgressStream
) -> ResearchResult:
    """Research a question to a cited report, blocking for minutes. Never call this on the loop."""
    started = time.monotonic()
    models = build_research_models(
        settings, options.model_fast, options.model_strong, options.model_reason
    )
    checker = build_checker(settings)
    if checker is not None and (options.ground is None or options.ground):
        models["ground"] = checker
    if checker is not None and options.ground_intake:
        models["ground_intake"] = checker
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
        started + options.research_minutes * 60,
        options.max_searches,
        options.results_per_query or settings.research.results_per_query,
        options.max_attempts or settings.research.max_attempts,
        options.max_depth or MAX_DEPTH,
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

    stream.publish(WRITE, "outlining the report")
    found = outline(models["outline"], encoder, brief, questions)
    titles = [title for title, _, _ in found]
    sections: list[tuple[str, str]] = []
    for done, (title, subsections, ids) in enumerate(found):
        stream.publish(WRITE, f"writing {title}", done, len(found))
        text = write.section(
            models["write"], encoder, brief, titles, title, sections, claims, ids, subsections
        )
        sections.append((title, text))
    stream.publish(WRITE, "summing up the report", len(found), len(found))
    summary = write.summary(models["tldr"], brief, sections)
    stream.publish(WRITE, "assembled the report", len(found), len(found))
    if left := open_questions(questions):
        sections.append(("Open questions", left))

    by_id = {claim.id: claim for claim in claims}
    # The summary is checked with the sections, so the checker is loaded once.
    checked = verify.report(models, [(SUMMARY, summary), *sections], by_id, stream)
    (_, summary), *sections = checked.sections
    cross_check(models, encoder, claims, stream, options.corroborate, options.contradict)
    report, citations = render(sections, by_id, summary)
    return result(
        report,
        citations=citations,
        removed=checked.removed,
        revised=checked.revised,
        unverified=checked.unverified,
    )


def outline(
    model: GatewayModel, encoder: Encoder, brief: str, questions: list[Question]
) -> list[tuple[str, list[str], set[int]]]:
    """Each section's title, its subsections, and the claims its questions rest on.

    At most `MAX_SECTIONS`, one per title. When the outline cannot be read, each branch is a
    section, titled by its first question. A question with claims that no section reports on adds
    them to the section whose title is closest to it.
    """
    held = [q for q in questions if q.claims]
    lines = []
    for q in held:
        line = f"[{q.id}] {q.title or q.text}: {q.text} ({q.status}, {len(q.claims)} claims)"
        lines.append(line + (f"; missing: {q.missing}" if q.missing else ""))
    by_id = {q.id: q for q in held}
    try:
        drafted = predict(model, Outline, brief=brief, questions="\n".join(lines)).sections
    except AdapterParseError as exc:
        logger.warning("could not read the outline: %s", exc)
        drafted = []
    by_branch: list[tuple[str, list[str], list[int]]] = [
        (branch[0].title or branch[0].text, [], [q.id for q in branch])
        for branch in branches(questions).values()
    ]
    # By title, lower-cased, so a repeated title adds to its first section.
    found: dict[str, tuple[str, list[str], set[int]]] = {}
    unused = set(by_id)
    usable = [
        (title, s.subsections, asked)
        for s in drafted
        if (title := s.title.strip()) and (asked := [q for q in s.questions if q in by_id])
    ]
    for title, subsections, asked in usable or by_branch:
        asked = [q for q in asked if q in by_id]
        if not asked or (title.lower() not in found and len(found) >= MAX_SECTIONS):
            continue
        _, subs, ids = found.setdefault(title.lower(), (title, [], set()))
        subs.extend(s.strip() for s in subsections if s.strip() and s.strip() not in subs)
        ids.update(i for q in asked for i in by_id[q].claims)
        unused -= set(asked)
    sections = list(found.values())
    if unused and sections:
        vectors = encoder.encode([title for title, _, _ in sections])
        for left in sorted(unused):
            [closest] = nearest(encoder, by_id[left].text, vectors, 1)
            sections[closest][2].update(by_id[left].claims)
    return sections


def open_questions(questions: list[Question]) -> str:
    """The first questions the research did not settle, then the raised ones it answered in part or
    found disputed. A raised question it found nothing for was a lead of its own, not the brief's.
    """
    left = [
        q
        for q in questions
        if q.status in UNSETTLED and (q.depth == 0 or q.status in ("weak", "contradicted"))
    ]
    return "\n".join(
        f"- {q.text} ({UNSETTLED[q.status] if q.attempts else 'not searched'})"
        for q in sorted(left, key=lambda q: q.depth > 0)[:OPEN_QUESTIONS]
    )


def _defaults(settings: Settings) -> dict[str, Any]:
    return {
        "results_per_query": settings.research.results_per_query,
        "max_attempts": settings.research.max_attempts,
        "max_depth": MAX_DEPTH,
        "ground": settings.model.checker is not None,
        "model_fast": settings.model.fast,
        "model_strong": settings.model.strong,
        "model_reason": settings.model.reason or settings.model.strong,
    }


GRAPH = Method(
    name="graph",
    Options=GraphOptions,
    clarifies=True,
    stages=lambda options: [BRIEF, RESEARCH, WRITE, VERIFY],
    defaults=_defaults,
    run=run_graph,
)
