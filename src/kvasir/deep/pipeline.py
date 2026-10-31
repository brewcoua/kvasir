"""Driving a deep research run.

Code decides how much to search and when to stop; the model only decides what to search for, what
a page says, and how to write it up. Every model call sees one small input, so a 16k context is
enough. Calls are sequential: a local server answers one at a time, so fanning out buys nothing.
"""

from __future__ import annotations

import datetime
import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator
from statistics import mean
from typing import Any, TypeVar

import dspy  # type: ignore[import-untyped]
from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.config import Settings
from kvasir.deep import verify
from kvasir.deep.fetch import FetchError, fetch_text, split
from kvasir.deep.render import matrix, render
from kvasir.deep.signatures import (
    Assess,
    Brief,
    CheckAnswer,
    Clarify,
    Criteria,
    Decompose,
    Entities,
    Intake,
    Outline,
    PlanQueries,
    RankCriterion,
    SearchFor,
    Verify,
    WriteSection,
)
from kvasir.deep.store import Claim, Question, closest, nearest
from kvasir.models import (
    Cell,
    Clarification,
    Criterion,
    RemovedSentence,
    ResearchClaim,
    ResearchQuestion,
    ResearchRequest,
    ResearchResult,
    Row,
)
from kvasir.progress import ASSESS, BACKGROUND, BRIEF, RESEARCH, VERIFY, WRITE, ProgressStream
from kvasir.runners import build_research_models, build_research_tools
from kvasir.storm import runtime
from kvasir.storm.encoder import Encoder, cosine_similarity
from kvasir.storm.lm import GatewayModel
from kvasir.storm.rm import SearXNG

# Pieces of one page kept by the embedding filter, the only ones a model reads.
PIECES_PER_PAGE = 3
# The query shape for each attempt at a question, the last repeated: a question the plain words
# do not answer gets the words its answer would contain, then a primary source, then the broader
# topic. Whether `site:` and `filetype:` work depends on the engines SearXNG uses.
QUERY_SHAPES = (
    "general: the plain words someone would search for",
    "specific: the exact terms, names, figures or year the answer would contain",
    "a primary source: add site: and the official domain, or filetype:pdf for a report or paper",
    "a step back: the broader topic the answer sits in",
)
# Questions the first split of the brief adds at most.
FIRST_QUESTIONS = 6
# Questions in the ledger at most. Follow-ups past this are dropped: the budget could not search
# for them anyway.
MAX_QUESTIONS = 24
# Follow-ups one answered question adds at most.
FOLLOW_UPS = 2
# Above this cosine similarity, a new question is taken to repeat one in the ledger.
DUPLICATE_QUESTION = 0.9
# Claims a question is judged on, the closest to it.
CLAIMS_PER_QUESTION = 8
# Searches in a row that add no claim before the research stops: one empty search is the cue to
# try another shape, not to stop, so this waits for several.
STALL_SEARCHES = 4
# Searches in a row that fail before the run stops, rather than write from no evidence.
FAILED_SEARCHES = 3
# Claims given to one section, the closest to its title.
CLAIMS_PER_SECTION = 15
# Sentences verification removed that are searched for again, at most, and the searches that may
# take. One pass: a rewritten section's own removals stand.
REPAIR_SENTENCES = 4
REPAIR_SEARCHES = 4
# How much of each earlier section a later one sees, to avoid repeating it without filling the
# context with it.
EARLIER_SECTION_CHARACTERS = 400
# Searches whose snippets the criteria and the options are chosen from.
BACKGROUND_QUERIES = 3
# Claims shown when assessing one option on one criterion, the closest to the pair.
CLAIMS_PER_CELL = 8
# Searches for the cells still empty after the research, on top of its budget.
FILL_SEARCHES = 8

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

logger = logging.getLogger(__name__)

# Judges a question on the claims gathered so far, updating its status, claims and what it misses.
Judge = Callable[[Question], None]
T = TypeVar("T")


class SearchUnavailable(RuntimeError):
    """Searches kept failing, so the run stops rather than write from no evidence."""

    def __init__(self, searches: int, failures: list[str]) -> None:
        self.failures = list(dict.fromkeys(failures))
        super().__init__(f"{searches} searches in a row failed: {'; '.join(self.failures)}")


def clarify(settings: Settings, question: str) -> list[str]:
    """Questions worth asking before researching `question`, or none."""
    model = build_research_models(settings)["clarify"]
    with dspy.context(lm=model):
        return list(dspy.Predict(Clarify)(question=question).questions[:3])


def run_research(
    settings: Settings, request: ResearchRequest, stream: ProgressStream
) -> ResearchResult:
    """Research a question to a cited report, blocking for minutes. Never call this on the loop."""
    started = time.monotonic()
    models = build_research_models(settings, request.model_fast, request.model_strong)
    encoder, search = build_research_tools(settings)
    budget = _setting(request.max_searches, settings.research_max_searches)
    per_query = _setting(request.results_per_query, settings.research_results_per_query)
    attempts = _setting(request.max_attempts, settings.research_max_attempts)
    follow_ups = _setting(request.follow_ups, settings.research_follow_ups)
    max_depth = _setting(request.max_depth, settings.research_max_depth)

    # The model's sense of "now" is its training cutoff, so "best" would mean best then.
    today = datetime.date.today().isoformat()

    stream.publish(BRIEF, "writing the research brief")
    framed = _predict(
        models["brief"],
        Brief,
        today=today,
        question=request.question,
        clarifications=_clarifications(request.clarifications),
    )
    brief: str = framed.brief
    output = request.output or framed.output
    seen: set[str] = set()

    # Named in the question, they are final; the request's own fields override even those. The
    # brief lists options from memory as readily as from the question, so only those the question
    # really names count as named.
    asked = f"{request.question}\n{_clarifications(request.clarifications)}"
    named = [name for name in framed.criteria if _names(name, asked)]
    criteria = [Criterion(name=name) for name in request.criteria or named]
    entities = list(request.entities or [e for e in framed.entities if _names(e, asked)])
    if output != "report":
        criteria, entities = _candidates(
            models,
            search,
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
    first = _predict(models["decompose"], Decompose, brief=brief).questions[:FIRST_QUESTIONS]
    _add_questions(encoder, ledger, first or [request.question])

    def check(question: Question) -> None:
        asked = _check(models["check"], encoder, brief, claims, question)
        if follow_ups and question.status == "answered" and question.depth < max_depth:
            _add_questions(encoder, ledger, asked[:FOLLOW_UPS], question)

    searched = 0

    def research(questions: list[Question], budget: int, judge: Judge) -> None:
        nonlocal searched
        searched += _research(
            models,
            encoder,
            search,
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
            questions=_questions(ledger),
            output=output,
            duration_seconds=round(time.monotonic() - started, 1),
        )

    rows: list[Row] = []
    if output != "report":
        rows = _assess(models, encoder, brief, claims, entities, criteria, stream)
        _fill(models["assess"], encoder, brief, claims, ledger, rows, research, stream)
        if output == "ranking":
            rows = _rank(models["judge"], brief, criteria, rows, stream)
            brief += f"\nOverall ranking, best first: {', '.join(row.entity for row in rows)}"

    stream.publish(WRITE, "outlining the report")
    titles: list[str] = _predict(
        models["outline"], Outline, brief=brief, shape=SHAPES[output], notes=_notes(ledger)
    ).sections
    sections: list[tuple[str, str]] = []
    for done, title in enumerate(titles):
        stream.publish(WRITE, f"writing {title}", done, len(titles))
        sections.append(
            (title, _write(models["write"], encoder, brief, titles, title, sections, claims))
        )
    stream.publish(WRITE, "assembled the report", len(titles), len(titles))
    if rows:
        sections.insert(0, (TABLE_TITLES[output], matrix(rows, criteria)))
    if left := _open_questions(ledger):
        sections.append(("Open questions", left))

    by_id = {claim.id: claim for claim in claims}
    sections, removed = _verify(models["verify"], sections, by_id, stream)

    def judge(question: Question) -> None:
        _check(models["check"], encoder, brief, claims, question)

    sections, removed = _repair(
        models, encoder, brief, titles, claims, sections, removed, research, judge, stream
    )

    by_id = {claim.id: claim for claim in claims}
    report, citations = render(sections, by_id)
    return ResearchResult(
        report=report,
        assumptions=list(framed.assumptions),
        citations=citations,
        claims=[ResearchClaim(**vars(claim)) for claim in claims],
        questions=_questions(ledger),
        removed=removed,
        output=output,
        criteria=criteria if rows else [],
        matrix=rows,
        duration_seconds=round(time.monotonic() - started, 1),
    )


def _write(
    model: GatewayModel,
    encoder: Encoder,
    brief: str,
    titles: list[str],
    title: str,
    earlier: list[tuple[str, str]],
    claims: list[Claim],
    extra: Iterable[int] = (),
) -> str:
    """One section, from the claims closest to its title and the `extra` claim ids."""
    shown = closest(encoder, f"{title}\n{brief}", [c.text for c in claims], CLAIMS_PER_SECTION)
    ids = {claims[i].id for i in shown} | set(extra)
    return str(
        _predict(
            model,
            WriteSection,
            brief=brief,
            outline="\n".join(titles),
            section=title,
            earlier_sections="\n\n".join(
                f"{t}: {body[:EARLIER_SECTION_CHARACTERS]}" for t, body in earlier
            ),
            evidence=_listed([c for c in claims if c.id in ids]),
        ).text
    )


def _repair(
    models: dict[str, GatewayModel],
    encoder: Encoder,
    brief: str,
    titles: list[str],
    claims: list[Claim],
    sections: list[tuple[str, str]],
    removed: list[RemovedSentence],
    research: Callable[[list[Question], int, Judge], None],
    judge: Judge,
    stream: ProgressStream,
) -> tuple[list[tuple[str, str]], list[RemovedSentence]]:
    """Search for what verification removed, and write again the sections the search supports.

    A removed sentence is often true, cited to a claim that does not say it. Each becomes a
    question; a section whose question the search answers is written again with the claims that
    answer it, and checked again.
    """
    picked = [r for r in removed if r.section in titles][:REPAIR_SENTENCES]
    if not picked:
        return sections, removed
    questions = [
        Question(id=n, text=verify.unmarked(r.sentence), missing=r.reason, judged_at=len(claims))
        for n, r in enumerate(picked, 1)
    ]
    stream.publish(VERIFY, f"searching again for {len(picked)} removed sentences")
    research(questions, REPAIR_SEARCHES, judge)
    found: dict[str, list[int]] = {}
    for sentence, question in zip(picked, questions, strict=True):
        if question.status == "answered":
            found.setdefault(sentence.section, []).extend(question.claims)
    stream.publish(VERIFY, f"found support for {len(found)} of the sections they came from")

    by_id = {claim.id: claim for claim in claims}
    sections = list(sections)
    for title, ids in found.items():
        index = next(i for i, (t, _) in enumerate(sections) if t == title)
        stream.publish(VERIFY, f"writing {title} again")
        text = _write(
            models["write"],
            encoder,
            brief,
            titles,
            title,
            [s for s in sections[:index] if s[0] in titles],
            claims,
            ids,
        )
        [(_, kept)], again = _verify(models["verify"], [(title, text)], by_id, stream)
        sections[index] = (title, kept)
        removed = [r for r in removed if r.section != title] + again
    return sections, removed


def _candidates(
    models: dict[str, GatewayModel],
    search: SearXNG,
    today: str,
    brief: str,
    criteria: list[Criterion],
    entities: list[str],
    max_criteria: int,
    max_entities: int,
    per_query: int,
    stream: ProgressStream,
) -> tuple[list[Criterion], list[str]]:
    """The criteria and the options to compare, chosen from search snippets where not given."""
    if not (criteria and entities):
        stream.publish(BACKGROUND, "searching for the options and what sets them apart")
        queries = _predict(
            models["plan"],
            PlanQueries,
            today=today,
            brief=brief,
            notes="",
            count=BACKGROUND_QUERIES,
        ).queries[:BACKGROUND_QUERIES]
        # Snippets only, and not marked seen: the research still reads these pages.
        background = "\n".join(
            f"{hit['title']}: {hit['description']}"
            for hit in _search(search, queries, per_query, set())
        )
        if not criteria:
            stream.publish(BACKGROUND, "choosing the criteria")
            chosen = _predict(
                models["criteria"], Criteria, brief=brief, background=background
            ).criteria
            criteria = [
                Criterion(name=c.name, weight=min(max(c.weight, 1), 3))
                for c in chosen[:max_criteria]
            ]
        if not entities:
            stream.publish(BACKGROUND, "choosing the options to compare")
            chosen = _predict(
                models["entities"], Entities, brief=brief, background=background, count=max_entities
            ).entities
            # A small model fills a list with plausible names, such as model sizes never released;
            # searching for those finds nothing. Only options the results mention are kept.
            entities = [entity for entity in chosen if _names(entity, background)][:max_entities]
    # The first spelling of a name given twice wins.
    names = [_key(c.name) for c in criteria]
    criteria = [c for i, c in enumerate(criteria) if names[i] and names[i] not in names[:i]]
    names = [_key(e) for e in entities]
    entities = [e.strip() for i, e in enumerate(entities) if names[i] and names[i] not in names[:i]]
    stream.publish(
        BACKGROUND,
        f"comparing {len(entities)} options on {len(criteria)} criteria: {', '.join(entities)}",
    )
    return criteria, entities


def _assess(
    models: dict[str, GatewayModel],
    encoder: Encoder,
    brief: str,
    claims: list[Claim],
    entities: list[str],
    criteria: list[Criterion],
    stream: ProgressStream,
) -> list[Row]:
    """Each option's finding on each criterion, from the claims the research gathered."""
    rows = [Row(entity=entity, cells=[]) for entity in entities]
    total = len(entities) * len(criteria)
    vectors = encoder.encode([c.text for c in claims])
    for done, (row, criterion) in enumerate((row, c) for row in rows for c in criteria):
        stream.publish(ASSESS, f"assessing {row.entity} on {criterion.name}", done, total)
        row.cells.append(
            _cell(models["assess"], encoder, vectors, brief, claims, row.entity, criterion.name)
        )
    return rows


def _fill(
    model: GatewayModel,
    encoder: Encoder,
    brief: str,
    claims: list[Claim],
    ledger: list[Question],
    rows: list[Row],
    research: Callable[[list[Question], int, Judge], None],
    stream: ProgressStream,
) -> None:
    """Search for the empty cells of `rows` as questions of their own, and assess them again.

    A cell's question is answered once assessing it finds a claim, so the assessment is its
    judgement and no other call is needed.
    """
    cells = {
        (row.entity, cell.criterion): (row, i) for row in rows for i, cell in enumerate(row.cells)
    }
    empty = [key for key, (row, i) in cells.items() if not row.cells[i].claims]
    if not empty:
        return
    questions = [
        Question(
            id=len(ledger) + n,
            text=f"{criterion} of {entity}",
            cell=(entity, criterion),
            judged_at=len(claims),
        )
        for n, (entity, criterion) in enumerate(empty, 1)
    ]
    ledger.extend(questions)

    def judge(question: Question) -> None:
        assert question.cell is not None
        row, i = cells[question.cell]
        vectors = encoder.encode([c.text for c in claims])
        row.cells[i] = _cell(model, encoder, vectors, brief, claims, *question.cell)
        question.judged_at = len(claims)
        question.claims = row.cells[i].claims
        question.status = "answered" if question.claims else "open"

    stream.publish(ASSESS, f"searching for {len(empty)} empty cells")
    research(questions, FILL_SEARCHES, judge)
    filled = sum(1 for key in empty if cells[key][0].cells[cells[key][1]].claims)
    stream.publish(ASSESS, f"filled {filled} of {len(empty)} empty cells")


def _cell(
    model: GatewayModel,
    encoder: Encoder,
    vectors: Any,
    brief: str,
    claims: list[Claim],
    entity: str,
    criterion: str,
) -> Cell:
    shown = [
        claims[i] for i in nearest(encoder, f"{entity}: {criterion}", vectors, CLAIMS_PER_CELL)
    ]
    try:
        assessed = _predict(
            model, Assess, brief=brief, entity=entity, criterion=criterion, evidence=_listed(shown)
        )
    except AdapterParseError as exc:
        logger.warning("could not read the finding on %s, %s: %s", entity, criterion, exc)
        return Cell(criterion=criterion, finding="", claims=[])
    # Only ids it was shown, of claims that name the entity: a small model otherwise cites another
    # option's figures for this one.
    ids = [
        c.id
        for c in shown
        if c.id in assessed.claims and _names(entity, f"{c.text} {c.quote} {c.title}")
    ]
    finding = str(assessed.finding).strip()
    if not (ids and finding):
        return Cell(criterion=criterion, finding="", claims=[])
    return Cell(criterion=criterion, finding=finding, claims=ids)


def _rank(
    model: GatewayModel,
    brief: str,
    criteria: list[Criterion],
    rows: list[Row],
    stream: ProgressStream,
) -> list[Row]:
    """`rows` best first, each ranked on every criterion twice, the second time in reverse order.

    A model favours the options it reads first; reversing the order and averaging cancels that.
    Only options with a finding are judged; those without one tie for the places after them, since
    a judge asked to rank them anyway places them by guesswork. The overall order is the weighted
    mean of the criterion ranks, computed here, not judged.
    """
    for index, criterion in enumerate(criteria):
        found = [row for row in rows if row.cells[index].claims]
        places: dict[str, list[int]] = {row.entity: [] for row in found}
        if len(found) == 1:
            places[found[0].entity].append(1)
        passes = (found, found[::-1]) if len(found) > 1 else ()
        for turn, order in enumerate(passes, 1):
            stream.publish(
                ASSESS,
                f"ranking on {criterion.name}, pass {turn} of 2",
                2 * index + turn - 1,
                2 * len(criteria),
            )
            options = "\n".join(f"{row.entity}: {row.cells[index].finding}" for row in order)
            try:
                ranking = _predict(
                    model, RankCriterion, brief=brief, criterion=criterion.name, options=options
                ).ranking
            except AdapterParseError as exc:
                # The input order stands in; the other pass, reversed, cancels it into a tie.
                logger.warning("could not read the ranking on %s: %s", criterion.name, exc)
                ranking = []
            for place, entity in enumerate(_ordered(ranking, [r.entity for r in order]), 1):
                places[entity].append(place)
        last = (len(found) + 1 + len(rows)) / 2
        for row in rows:
            row.cells[index].rank = mean(places[row.entity]) if row.entity in places else last

    weight = sum(c.weight for c in criteria)
    scores = {
        row.entity: sum(
            c.weight * (cell.rank or 0) for c, cell in zip(criteria, row.cells, strict=True)
        )
        / weight
        for row in rows
    }
    ranked = sorted(rows, key=lambda row: scores[row.entity])
    for place, row in enumerate(ranked, 1):
        row.rank = place
    stream.publish(
        ASSESS,
        f"ranked: {', '.join(row.entity for row in ranked)}",
        2 * len(criteria),
        2 * len(criteria),
    )
    return ranked


def _research(
    models: dict[str, GatewayModel],
    encoder: Encoder,
    search: SearXNG,
    today: str,
    brief: str,
    questions: list[Question],
    claims: list[Claim],
    seen: set[str],
    budget: int,
    per_query: int,
    max_attempts: int,
    judge: Judge,
    first_step: int,
    stream: ProgressStream,
) -> int:
    """Search for `questions` one at a time, and return how many searches that took.

    Stops when `budget` searches are spent, no question is left to search for, or
    `STALL_SEARCHES` searches in a row find nothing. A question is judged again before it is
    searched for whenever claims arrived since its last judgement, since a search for one question
    often answers another. Adds what it finds to `claims`; `judge` may add to `questions`.
    """
    searches = stalled = failed = 0
    failures: list[str] = []
    while searches < budget and stalled < STALL_SEARCHES:
        question = _pick(questions, max_attempts)
        if question is None:
            break
        if question.judged_at < len(claims):
            judge(question)
            continue
        searches += 1
        stream.publish(RESEARCH, f"search {searches} of at most {budget}, for: {question.text}")
        query = str(
            _predict(
                models["plan"],
                SearchFor,
                today=today,
                brief=brief,
                question=question.text,
                tried="\n".join(question.queries) or "none",
                missing=question.missing,
                shape=QUERY_SHAPES[min(question.attempts, len(QUERY_SHAPES) - 1)],
            ).query
        ).strip()
        try:
            hits = _search(search, [query], per_query, seen)
        except SearchUnavailable as exc:
            # The attempt is not spent, since the query was never answered; the budget is.
            failed += 1
            failures += exc.failures
            if failed == FAILED_SEARCHES:
                raise SearchUnavailable(failed, failures) from None
            continue
        failed = 0
        failures = []
        question.queries.append(query)
        new = _read(
            models["intake"],
            encoder,
            brief,
            question.text,
            hits,
            len(claims) + 1,
            first_step + searches,
            f"search {searches}",
            stream,
        )
        claims.extend(new)
        stalled = 0 if new else stalled + 1
        if new:
            judge(question)
        if question.status == "open" and question.attempts >= max_attempts:
            question.status = "unresolved"
        stream.publish(
            RESEARCH,
            f"search {searches}: {len(new)} new claims, {len(claims)} in all; "
            f"{question.text}: {question.status}",
        )
    return searches


def _pick(questions: list[Question], max_attempts: int) -> Question | None:
    """The question to search for next: the least tried of those not settled, shallowest first.

    Least tried first, so every question gets a plain search before any gets a narrower one.
    """
    waiting = [
        q
        for q in questions
        if q.status in ("open", "weak", "contradicted") and q.attempts < max_attempts
    ]
    return min(waiting, key=lambda q: (q.attempts, q.depth, q.id), default=None)


def _check(
    model: GatewayModel, encoder: Encoder, brief: str, claims: list[Claim], question: Question
) -> list[str]:
    """Judge `question` on its closest claims, and return the harder questions the judge asks.

    A claim whose quote does not say what it claims is not counted, so a misquoted answer leaves
    the question open and the next search looks for it again.
    """
    previous, question.judged_at = question.judged_at, len(claims)
    shown = [
        claims[i]
        for i in closest(encoder, question.text, [c.text for c in claims], CLAIMS_PER_QUESTION)
    ]
    # Ids count up from 1 as claims arrive, so none above `previous` means the closest claims are
    # the ones it was last judged on, and the verdict would not change.
    if all(c.id <= previous for c in shown):
        return []
    try:
        judged = _predict(
            model, CheckAnswer, brief=brief, question=question.text, evidence=_quoted_claims(shown)
        )
    except AdapterParseError as exc:
        logger.warning("could not read the judgement on %r: %s", question.text, exc)
        return []
    # Only ids it was shown: a small model otherwise cites claims from its own numbering.
    ids = [c.id for c in shown if c.id in judged.claims]
    if judged.status == "open" or not ids:
        question.status, question.claims = "open", []
    else:
        question.status, question.claims = judged.status, ids
    question.missing = "" if question.status == "answered" else str(judged.missing).strip()
    return [str(q) for q in judged.follow_ups] if question.status == "answered" else []


def _add_questions(
    encoder: Encoder, ledger: list[Question], texts: list[str], parent: Question | None = None
) -> None:
    """Add to `ledger` those of `texts` that repeat no question in it, up to `MAX_QUESTIONS`."""
    texts = [text.strip() for text in texts if text.strip()]
    if not texts:
        return
    vectors = list(encoder.encode([q.text for q in ledger] + texts))
    kept = vectors[: len(ledger)]
    for text, vector in zip(texts, vectors[len(ledger) :], strict=True):
        if len(ledger) >= MAX_QUESTIONS:
            return
        if kept and cosine_similarity([vector], kept).max() > DUPLICATE_QUESTION:
            continue
        ledger.append(
            Question(
                id=len(ledger) + 1,
                text=text,
                parent=parent.id if parent else None,
                depth=parent.depth + 1 if parent else 0,
            )
        )
        kept.append(vector)


def _notes(ledger: list[Question]) -> str:
    """The ledger as the outline reads it: each question, how far it got, and its claims."""
    lines = []
    for question in ledger:
        if question.cell:
            continue
        line = f"- {question.text} ({question.status})"
        if question.claims:
            line += " " + "".join(f"[{i}]" for i in question.claims)
        if question.missing:
            line += f"; missing: {question.missing}"
        lines.append(line)
    return "\n".join(lines)


# How the report names a question's status when the research did not settle it.
UNSETTLED = {
    "open": "not found",
    "unresolved": "not found",
    "weak": "partly answered",
    "contradicted": "sources disagree",
}


def _open_questions(ledger: list[Question]) -> str:
    """The questions the research did not settle, as a list for the end of the report.

    Empty cells are left out: the table already shows them as a dash. So are follow-ups the budget
    never reached, which were only suggestions; a first question it never reached says so.
    """
    return "\n".join(
        f"- {q.text} ({UNSETTLED[q.status] if q.attempts else 'not searched'})"
        for q in ledger
        if q.status in UNSETTLED and q.cell is None and (q.attempts or q.depth == 0)
    )


def _questions(ledger: list[Question]) -> list[ResearchQuestion]:
    return [
        ResearchQuestion(
            id=q.id,
            text=q.text,
            parent=q.parent,
            depth=q.depth,
            status=q.status,
            claims=q.claims,
            queries=q.queries,
            missing=q.missing,
        )
        for q in ledger
    ]


def _setting(value: T | None, default: T) -> T:
    """The request's value, or the configured default when the request leaves it out."""
    return default if value is None else value


def _names(entity: str, text: str) -> bool:
    """Whether `text` names `entity`: most of the words of its name appear in it.

    Most rather than all, since sources shorten names: "Qwen3-Coder-30B" for "Qwen3-Coder-30B-A3B".
    """
    words = _words(entity).split()
    found = sum(f" {word} " in _words(text) for word in words)
    return bool(words) and found * 2 > len(words)


def _key(name: str) -> str:
    return name.strip().lower()


def _ordered(ranking: list[str], names: list[str]) -> list[str]:
    """`names` in the order `ranking` gives them, those it leaves out last, in their own order.

    Models number the list and change the case, so a name is matched without either.
    """
    known = {name.lower(): name for name in names}
    found = [
        known[key]
        for item in ranking
        if (key := re.sub(r"^\s*\d+[.)]\s*", "", item).strip().lower()) in known
    ]
    found = list(dict.fromkeys(found))
    return found + [name for name in names if name not in found]


def _verify(
    model: GatewayModel,
    sections: list[tuple[str, str]],
    claims: dict[int, Claim],
    stream: ProgressStream,
) -> tuple[list[tuple[str, str]], list[RemovedSentence]]:
    total = sum(verify.count(text) for _, text in sections)
    done = 0
    stream.publish(VERIFY, f"checking {total} cited sentences against their quotes", 0, total)

    def check(sentence: str, evidence: list[Claim]) -> tuple[bool, str]:
        try:
            verdict = _predict(model, Verify, sentence=sentence, evidence=_quoted_claims(evidence))
        except AdapterParseError as exc:
            # Unchecked rather than removed: a verdict that cannot be read says nothing about
            # the sentence.
            logger.warning("could not read the verdict on %r: %s", sentence, exc)
            return True, ""
        return bool(verdict.supported), str(verdict.reason)

    def checked() -> None:
        nonlocal done
        done += 1
        stream.publish(VERIFY, f"checked {done} of {total} cited sentences", done, total)

    verified: list[tuple[str, str]] = []
    removed: list[RemovedSentence] = []
    for title, text in sections:
        kept, dropped = verify.verify(title, text, claims, check, checked)
        verified.append((title, kept))
        removed.extend(dropped)
    stream.publish(VERIFY, f"removed {len(removed)} of {total} cited sentences", total, total)
    return verified, removed


def _quoted_claims(claims: list[Claim]) -> str:
    return "\n".join(f"[{claim.id}] {claim.text}\n    quote: {claim.quote}" for claim in claims)


def _predict(model: GatewayModel, signature: type[dspy.Signature], **inputs: Any) -> Any:
    with dspy.context(lm=model):
        return dspy.Predict(signature)(**inputs)


def _clarifications(clarifications: list[Clarification]) -> str:
    if not clarifications:
        return "none"
    return "\n".join(f"Q: {c.question}\nA: {c.answer}" for c in clarifications)


def _listed(claims: list[Claim]) -> str:
    return "\n".join(f"[{claim.id}] {claim.text}" for claim in claims)


def _search(
    search: SearXNG, queries: list[str], per_query: int, seen: set[str]
) -> list[dict[str, Any]]:
    """Results not read before, at most `per_query` from each query. Marks them seen."""
    hits = []
    failures: list[str] = []
    for query in queries:
        runtime.record_search_usage("SearXNG", 1)
        found = [hit for hit in search(query, exclude_urls=list(seen)) if hit["url"] not in seen]
        failures += search.failures
        for hit in found[:per_query]:
            seen.add(hit["url"])
            hits.append(hit)
    if not hits and failures:
        raise SearchUnavailable(len(queries), failures)
    return hits


def _fetch_all(hits: list[dict[str, Any]]) -> Iterator[str]:
    """Each page's text, or its search snippet when the page cannot be read."""

    def fetch(hit: dict[str, Any]) -> str:
        try:
            return fetch_text(hit["url"])
        except FetchError as exc:
            logger.info("falling back to the snippet: %s", exc)
            return str(hit["description"])

    with runtime.ContextThreadPoolExecutor(max_workers=runtime.max_threads()) as pool:
        yield from pool.map(fetch, hits)


def _read(
    model: GatewayModel,
    encoder: Encoder,
    brief: str,
    question: str,
    hits: list[dict[str, Any]],
    first_id: int,
    step: int,
    label: str,
    stream: ProgressStream,
) -> list[Claim]:
    """The claims in the pieces of each page closest to `question`, the one it was found for.

    Kept by the question rather than the brief: a page found for one narrow fact holds it in one
    piece, which the brief would not rank first.
    """
    pages = [
        (hit, _relevant_pieces(encoder, question, text))
        for hit, text in zip(hits, _fetch_all(hits), strict=True)
    ]
    pieces = [
        (page, part, hit, piece)
        for page, (hit, parts) in enumerate(pages, 1)
        for part, piece in enumerate(parts, 1)
    ]
    new: list[Claim] = []
    # Intake is most of a run's time, so each piece is its own event: the page being read is what
    # a follower wants to see.
    for done, (page, part, hit, piece) in enumerate(pieces):
        stream.publish(
            RESEARCH,
            f"{label}: page {page}/{len(pages)}, "
            f"part {part}/{len(pages[page - 1][1])}: {hit['title'] or hit['url']}",
            done,
            len(pieces),
        )
        new.extend(_intake(model, brief, question, hit, piece, first_id + len(new), step))
    return new


def _relevant_pieces(encoder: Encoder, query: str, text: str) -> list[str]:
    pieces = split(text)
    return [pieces[i] for i in closest(encoder, query, pieces, PIECES_PER_PAGE)]


def _intake(
    model: GatewayModel,
    brief: str,
    question: str,
    hit: dict[str, Any],
    piece: str,
    first_id: int,
    step: int,
) -> list[Claim]:
    """Claims whose quote really is in the piece. A paraphrased quote is not evidence."""
    try:
        findings = _predict(
            model, Intake, brief=brief, question=question, title=hit["title"], text=piece
        ).findings
    except AdapterParseError as exc:
        logger.warning("could not read the claims from %s: %s", hit["url"], exc)
        return []
    claims: list[Claim] = []
    for finding in findings:
        if not _quoted(finding.quote, piece):
            logger.info("dropped a claim whose quote is not on %s: %r", hit["url"], finding.quote)
            continue
        claims.append(
            Claim(
                id=first_id + len(claims),
                text=finding.claim,
                quote=finding.quote,
                url=hit["url"],
                title=hit["title"],
                confidence=finding.confidence,
                step=step,
            )
        )
    return claims


def _quoted(quote: str, text: str) -> bool:
    """Whether `quote` is in `text` word for word, an ellipsis standing for words left out.

    Words only: models write a non-breaking hyphen where the page has a hyphen, join list items
    without their bullets, and the extracted text has a break wherever the page had an inline tag,
    as in "attention , a". None of that changes what the quote says.
    """
    haystack = _words(text)
    fragments = [_words(part) for part in re.split(r"\.\.\.|…", quote)]
    fragments = [fragment for fragment in fragments if fragment]
    if not fragments:
        return False
    position = 0
    for fragment in fragments:
        found = haystack.find(fragment, position)
        if found < 0:
            return False
        # Back one, so the next fragment can start at the space this one ended on.
        position = found + len(fragment) - 1
    return True


def _words(text: str) -> str:
    # Padded, so a fragment matches whole words only: "4 bit" is not found in "14 bits".
    words = re.findall(r"\w+", text.lower())
    return f" {' '.join(words)} "
