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
from typing import Any

import dspy  # type: ignore[import-untyped]
from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.config import Settings
from kvasir.deep.fetch import FetchError, fetch_text, split
from kvasir.deep.render import render
from kvasir.deep.signatures import (
    Brief,
    Clarify,
    Intake,
    Outline,
    PlanQueries,
    RebuildNotes,
    WriteSection,
)
from kvasir.deep.store import Claim, closest
from kvasir.models import Clarification, ResearchClaim, ResearchRequest, ResearchResult
from kvasir.progress import BRIEF, RESEARCH, WRITE, ProgressStream
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

NO_EVIDENCE = (
    "## No findings\n\nNo page read during the research supported a claim about this question. "
    "Try rephrasing it, or answering the clarifying questions."
)

logger = logging.getLogger(__name__)


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

    claims: list[Claim] = []
    notes = ""
    seen: set[str] = set()
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
            duration_seconds=round(time.monotonic() - started, 1),
        )

    stream.publish(WRITE, "outlining the report")
    titles: list[str] = _predict(models["outline"], Outline, brief=brief, notes=notes).sections
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

    report, citations = render(sections, {claim.id: claim for claim in claims})
    return ResearchResult(
        report=report,
        assumptions=list(framed.assumptions),
        citations=citations,
        claims=[ResearchClaim(**vars(claim)) for claim in claims],
        duration_seconds=round(time.monotonic() - started, 1),
    )


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
    for query in queries:
        runtime.record_search_usage("SearXNG", 1)
        found = [hit for hit in search(query, exclude_urls=list(seen)) if hit["url"] not in seen]
        for hit in found[:per_query]:
            seen.add(hit["url"])
            hits.append(hit)
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
