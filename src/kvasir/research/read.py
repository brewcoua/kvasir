"""Reading the pages a search found into claims, each with a quote that is on its page.

With a grounding checker, each claim must also be supported by the piece of the page it was read
from: tier 2 of verification, after the quote check that is tier 1.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.research.calls import predict
from kvasir.research.claims import Claim, closest
from kvasir.research.fetch import FetchError, fetch_text, split
from kvasir.research.ground import grounded
from kvasir.research.signatures import Intake
from kvasir.research.text import quoted
from kvasir.runs.progress import RESEARCH, ProgressStream
from kvasir.storm import runtime
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel

# Pieces of one page kept by the embedding filter, the only ones a model reads.
PIECES_PER_PAGE = 3

logger = logging.getLogger(__name__)


def read(
    model: GatewayModel,
    encoder: Encoder,
    brief: str,
    question: str,
    hits: list[dict[str, Any]],
    first_id: int,
    step: int,
    label: str,
    stream: ProgressStream,
    checker: GatewayModel | None = None,
) -> list[Claim]:
    """The claims in the pieces of each page closest to `question`, the one it was found for.

    Kept by the question rather than the brief: a page found for one narrow fact holds it in one
    piece, which the brief would not rank first.
    """
    pages = [
        (hit, relevant_pieces(encoder, question, text))
        for hit, text in zip(hits, fetch_all(hits), strict=True)
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
        new.extend(intake(model, brief, question, hit, piece, first_id + len(new), step, checker))
    return new


def fetch_all(hits: list[dict[str, Any]]) -> Iterator[str]:
    """Each page's text, or its search snippet when the page cannot be read."""

    def fetch(hit: dict[str, Any]) -> str:
        try:
            return fetch_text(hit["url"])
        except FetchError as exc:
            logger.info("falling back to the snippet: %s", exc)
            return str(hit["description"])

    with runtime.ContextThreadPoolExecutor(max_workers=runtime.max_threads()) as pool:
        yield from pool.map(fetch, hits)


def relevant_pieces(encoder: Encoder, query: str, text: str) -> list[str]:
    pieces = split(text)
    return [pieces[i] for i in closest(encoder, query, pieces, PIECES_PER_PAGE)]


def intake(
    model: GatewayModel,
    brief: str,
    question: str,
    hit: dict[str, Any],
    piece: str,
    first_id: int,
    step: int,
    checker: GatewayModel | None = None,
) -> list[Claim]:
    """Claims whose quote really is in the piece. A paraphrased quote is not evidence.

    Checked against the piece rather than the quote alone: a claim names what its quote leaves to
    the surrounding text, such as the subject of "it was found in 1904".
    """
    try:
        findings = predict(
            model, Intake, brief=brief, question=question, title=hit["title"], text=piece
        ).findings
    except AdapterParseError as exc:
        logger.warning("could not read the claims from %s: %s", hit["url"], exc)
        return []
    claims: list[Claim] = []
    for finding in findings:
        if not quoted(finding.quote, piece):
            logger.info("dropped a claim whose quote is not on %s: %r", hit["url"], finding.quote)
            continue
        if checker is not None and not grounded(checker, piece, finding.claim):
            logger.info("dropped a claim %s does not support: %r", hit["url"], finding.claim)
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
