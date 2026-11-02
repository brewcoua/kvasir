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
from kvasir.research.text import misleading, quoted
from kvasir.runs.progress import RESEARCH, ProgressStream
from kvasir.storm import runtime
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel

# Pieces of one page kept by the embedding filter, the only ones a model reads.
PIECES_PER_PAGE = 3
# What an intake call holds besides the page, and how many characters a token is, from the 47
# intake calls of one run on gpt-oss: prompt tokens came to 34 + characters / 4.07, with brief,
# instructions and title about 600. Both are rounded towards a smaller page, as is the margin: a
# token holds fewer characters in figures, PDFs and languages other than English.
PROMPT_TOKENS = 1000
CHARACTERS_PER_TOKEN = 3.5
MARGIN = 0.9
# Joins the pieces of a page read in one call. They need not be adjacent on the page, so they are
# marked apart, and a claim is grounded on the one piece its quote is in.
GAP = "\n\n[...]\n\n"

logger = logging.getLogger(__name__)


def answer_tokens(context_tokens: int) -> int:
    """The room an intake call's answer gets: a quarter of the context. The same run's answers
    peaked at about 500 tokens for one piece, and one call now reads up to five."""
    return context_tokens // 4


def page_characters(context_tokens: int) -> int:
    """How much of a page one intake call reads: what the context holds once the prompt and the
    answer are taken out, less the margin."""
    tokens = context_tokens - PROMPT_TOKENS - answer_tokens(context_tokens)
    return int(tokens * MARGIN * CHARACTERS_PER_TOKEN)


def pack(pieces: list[str], size: int) -> list[str]:
    """`pieces` joined into as few texts of at most `size` characters as their order allows. A
    piece longer than `size` is a text of its own."""
    texts: list[list[str]] = []
    for piece in pieces:
        if texts and len(GAP.join([*texts[-1], piece])) <= size:
            texts[-1].append(piece)
        else:
            texts.append([piece])
    return [GAP.join(text) for text in texts]


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
        (hit, relevant_pieces(encoder, question, text), from_snippet)
        for hit, (text, from_snippet) in zip(hits, fetch_all(hits), strict=True)
    ]
    size = page_characters(runtime.context_tokens())
    texts = [
        (page, hit, text, from_snippet)
        for page, (hit, parts, from_snippet) in enumerate(pages, 1)
        for text in pack(parts, size)
    ]

    def take(call: tuple[int, dict[str, Any], str, bool]) -> list[Claim]:
        _, hit, text, from_snippet = call
        return intake(model, brief, question, hit, text, 0, step, checker, from_snippet)

    new: list[Claim] = []
    # Intake is most of a run's time, so its calls run side by side, as wide as the pools, and each
    # is its own event: the page read is what a follower wants to see.
    with runtime.ContextThreadPoolExecutor(max_workers=runtime.max_threads()) as pool:
        for done, ((page, hit, _, _), found) in enumerate(
            zip(texts, pool.map(take, texts), strict=True), 1
        ):
            stream.publish(
                RESEARCH,
                f"{label}: read page {page}/{len(pages)}: {hit['title'] or hit['url']}",
                done,
                len(texts),
            )
            # Numbered in page order once read, however the calls finished.
            for claim in found:
                claim.id = first_id + len(new)
                new.append(claim)
    return new


def fetch_all(hits: list[dict[str, Any]]) -> Iterator[tuple[str, bool]]:
    """Each page's text, or its search snippet when the page cannot be read, and whether it is the
    snippet."""

    def fetch(hit: dict[str, Any]) -> tuple[str, bool]:
        try:
            return fetch_text(hit["url"]), False
        except FetchError as exc:
            logger.info("falling back to the snippet: %s", exc)
            return str(hit["description"]), True

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
    from_snippet: bool = False,
) -> list[Claim]:
    """Claims whose quote really is in the piece. A paraphrased quote is not evidence, nor is one
    that a negator or qualifier just before it, or inside its ellipsis, turns around.

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
        # The piece of the page its quote is in, which is all a small checker has room for.
        source = next((p for p in piece.split(GAP) if quoted(finding.quote, p)), piece)
        if misleading(finding.quote, source):
            logger.info("dropped a claim whose quote leaves out a negation: %r", finding.quote)
            continue
        if checker is not None and not grounded(checker, source, finding.claim):
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
                from_snippet=from_snippet,
            )
        )
    return claims
