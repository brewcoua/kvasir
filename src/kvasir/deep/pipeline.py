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
from collections.abc import Iterator
from statistics import mean
from typing import Any

import dspy  # type: ignore[import-untyped]
from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.config import Settings
from kvasir.deep import verify
from kvasir.deep.fetch import FetchError, fetch_text, split
from kvasir.deep.render import matrix, render
from kvasir.deep.signatures import (
    Assess,
    Brief,
    Clarify,
    Criteria,
    Entities,
    Intake,
    Outline,
    PlanQueries,
    RankCriterion,
    RebuildNotes,
    Verify,
    WriteSection,
)
from kvasir.deep.store import Claim, closest, nearest
from kvasir.models import (
    Cell,
    Clarification,
    Criterion,
    RemovedSentence,
    ResearchClaim,
    ResearchRequest,
    ResearchResult,
    Row,
)
from kvasir.progress import ASSESS, BACKGROUND, BRIEF, RESEARCH, VERIFY, WRITE, ProgressStream
from kvasir.runners import build_research_models, build_research_tools
from kvasir.storm import runtime
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel
from kvasir.storm.rm import SearXNG

MAX_ROUNDS = 4
QUERIES_PER_ROUND = 4
RESULTS_PER_QUERY = 3
# Pieces of one page kept by the embedding filter, the only ones a model reads.
PIECES_PER_PAGE = 3
# A round that adds fewer claims than this has saturated what searching can find.
MIN_NEW_CLAIMS = 5
# Claims given to one section, the closest to its title.
CLAIMS_PER_SECTION = 15
# How much of each earlier section a later one sees, to avoid repeating it without filling the
# context with it.
EARLIER_SECTION_CHARACTERS = 400
# Options compared at most, when the run chooses them: assessing costs a call per option and
# criterion, and ranking two per criterion.
ENTITIES = 8
# Criteria at most, when the run chooses them: each costs a call per option and two rankings.
CRITERIA = 5
# Searches whose snippets the criteria and the options are chosen from.
BACKGROUND_QUERIES = 3
# Claims shown when assessing one option on one criterion, the closest to the pair.
CLAIMS_PER_CELL = 8
# Empty cells searched for again, at most, so filling cannot take longer than the research did.
FILL_CELLS = 8

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


class SearchUnavailable(RuntimeError):
    """Every search of a step failed, so the run stops rather than write from no evidence."""


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
    max_rounds = request.max_rounds or MAX_ROUNDS

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
        criteria, entities = _candidates(models, search, today, brief, criteria, entities, stream)
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
    notes = ""
    for round_number in range(1, max_rounds + 1):
        stream.publish(RESEARCH, f"round {round_number} of at most {max_rounds}: planning searches")
        queries = _predict(
            models["plan"],
            PlanQueries,
            today=today,
            brief=brief,
            notes=notes,
            count=request.queries_per_round or QUERIES_PER_ROUND,
        ).queries[: request.queries_per_round or QUERIES_PER_ROUND]

        hits = _search(search, queries, request.results_per_query or RESULTS_PER_QUERY, seen)
        stream.publish(
            RESEARCH,
            f"round {round_number}: fetching {len(hits)} pages from {len(queries)} searches",
        )
        pages = [
            (hit, _relevant_pieces(encoder, brief, text))
            for hit, text in zip(hits, _fetch_all(hits), strict=True)
        ]
        pieces = [
            (page, part, hit, piece)
            for page, (hit, parts) in enumerate(pages, 1)
            for part, piece in enumerate(parts, 1)
        ]
        new: list[Claim] = []
        # Intake is most of a run's time, so each piece is its own event: the page being read is
        # what a follower wants to see, not the round.
        for done, (page, part, hit, piece) in enumerate(pieces):
            stream.publish(
                RESEARCH,
                f"round {round_number}: page {page}/{len(pages)}, "
                f"part {part}/{len(pages[page - 1][1])}: {hit['title'] or hit['url']}",
                done,
                len(pieces),
            )
            new.extend(
                _intake(
                    models["intake"], brief, hit, piece, len(claims) + len(new) + 1, round_number
                )
            )

        claims.extend(new)
        stream.publish(
            RESEARCH,
            f"round {round_number}: {len(new)} new claims, {len(claims)} in all",
            len(pieces),
            len(pieces),
        )
        if new:
            stream.publish(RESEARCH, f"round {round_number}: rebuilding the notes")
            notes = _predict(
                models["notes"], RebuildNotes, brief=brief, notes=notes, new_claims=_listed(new)
            ).notes_out
        if len(new) < MIN_NEW_CLAIMS:
            break

    if not claims:
        # Writing sections from no evidence only produces paragraphs saying there is none.
        stream.publish(WRITE, "no evidence found, so nothing to write")
        return ResearchResult(
            report=NO_EVIDENCE,
            assumptions=list(framed.assumptions),
            citations=[],
            claims=[],
            output=output,
            duration_seconds=round(time.monotonic() - started, 1),
        )

    rows: list[Row] = []
    if output != "report":
        rows = _assess(models, encoder, search, brief, claims, entities, criteria, seen, stream)
        if output == "ranking":
            rows = _rank(models["judge"], brief, criteria, rows, stream)
            brief += f"\nOverall ranking, best first: {', '.join(row.entity for row in rows)}"

    stream.publish(WRITE, "outlining the report")
    titles: list[str] = _predict(
        models["outline"], Outline, brief=brief, shape=SHAPES[output], notes=notes
    ).sections
    sections: list[tuple[str, str]] = []
    for done, title in enumerate(titles):
        stream.publish(WRITE, f"writing {title}", done, len(titles))
        evidence = [
            claims[i]
            for i in closest(
                encoder, f"{title}\n{brief}", [c.text for c in claims], CLAIMS_PER_SECTION
            )
        ]
        text = _predict(
            models["write"],
            WriteSection,
            brief=brief,
            outline="\n".join(titles),
            section=title,
            earlier_sections="\n\n".join(
                f"{t}: {body[:EARLIER_SECTION_CHARACTERS]}" for t, body in sections
            ),
            evidence=_listed(evidence),
        ).text
        sections.append((title, text))
    stream.publish(WRITE, "assembled the report", len(titles), len(titles))
    if rows:
        sections.insert(0, (TABLE_TITLES[output], matrix(rows, criteria)))

    by_id = {claim.id: claim for claim in claims}
    sections, removed = _verify(models["verify"], sections, by_id, stream)

    report, citations = render(sections, by_id)
    return ResearchResult(
        report=report,
        assumptions=list(framed.assumptions),
        citations=citations,
        claims=[ResearchClaim(**vars(claim)) for claim in claims],
        removed=removed,
        output=output,
        criteria=criteria if rows else [],
        matrix=rows,
        duration_seconds=round(time.monotonic() - started, 1),
    )


def _candidates(
    models: dict[str, GatewayModel],
    search: SearXNG,
    today: str,
    brief: str,
    criteria: list[Criterion],
    entities: list[str],
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
            for hit in _search(search, queries, RESULTS_PER_QUERY, set())
        )
        if not criteria:
            stream.publish(BACKGROUND, "choosing the criteria")
            chosen = _predict(
                models["criteria"], Criteria, brief=brief, background=background
            ).criteria
            criteria = [
                Criterion(name=c.name, weight=min(max(c.weight, 1), 3)) for c in chosen[:CRITERIA]
            ]
        if not entities:
            stream.publish(BACKGROUND, "choosing the options to compare")
            chosen = _predict(
                models["entities"], Entities, brief=brief, background=background, count=ENTITIES
            ).entities
            # A small model fills a list with plausible names, such as model sizes never released;
            # searching for those finds nothing. Only options the results mention are kept.
            entities = [entity for entity in chosen if _names(entity, background)][:ENTITIES]
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
    search: SearXNG,
    brief: str,
    claims: list[Claim],
    entities: list[str],
    criteria: list[Criterion],
    seen: set[str],
    stream: ProgressStream,
) -> list[Row]:
    """Each option's finding on each criterion, searching again for some of those with none.

    Adds the claims the searching finds to `claims`.
    """
    rows = [Row(entity=entity, cells=[]) for entity in entities]
    total = len(entities) * len(criteria)
    vectors = encoder.encode([c.text for c in claims])
    for done, (row, criterion) in enumerate((row, c) for row in rows for c in criteria):
        stream.publish(ASSESS, f"assessing {row.entity} on {criterion.name}", done, total)
        row.cells.append(
            _cell(models["assess"], encoder, vectors, brief, claims, row.entity, criterion.name)
        )

    empty = [(row, i) for row in rows for i, cell in enumerate(row.cells) if not cell.claims]
    empty = empty[:FILL_CELLS]
    for done, (row, i) in enumerate(empty):
        query = f"{row.entity} {row.cells[i].criterion}"
        stream.publish(ASSESS, f"searching again for {query}", done, len(empty))
        hits = _search(search, [query], 1, seen)
        for hit, text in zip(hits, _fetch_all(hits), strict=True):
            for piece in _relevant_pieces(encoder, query, text):
                claims.extend(
                    _intake(models["intake"], brief, hit, piece, len(claims) + 1, round_number=0)
                )
        vectors = encoder.encode([c.text for c in claims])
        row.cells[i] = _cell(
            models["assess"], encoder, vectors, brief, claims, row.entity, row.cells[i].criterion
        )
    if empty:
        filled = sum(1 for row, i in empty if row.cells[i].claims)
        stream.publish(
            ASSESS, f"filled {filled} of {len(empty)} empty cells", len(empty), len(empty)
        )
    return rows


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
        raise SearchUnavailable(
            f"all {len(queries)} searches failed: {'; '.join(dict.fromkeys(failures))}"
        )
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


def _relevant_pieces(encoder: Encoder, brief: str, text: str) -> list[str]:
    pieces = split(text)
    return [pieces[i] for i in closest(encoder, brief, pieces, PIECES_PER_PAGE)]


def _intake(
    model: GatewayModel,
    brief: str,
    hit: dict[str, Any],
    piece: str,
    first_id: int,
    round_number: int,
) -> list[Claim]:
    """Claims whose quote really is in the piece. A paraphrased quote is not evidence."""
    try:
        findings = _predict(model, Intake, brief=brief, title=hit["title"], text=piece).findings
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
                round=round_number,
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
