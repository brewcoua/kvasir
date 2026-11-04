"""Reading the pages a search found into claims, each with a quote that is on its page.

With a grounding checker, each claim must also be supported by the piece of the page it was read
from: tier 2 of verification, after the quote check that is tier 1.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]
from pydantic import ValidationError

from kvasir.research.calls import predict
from kvasir.research.claims import Claim
from kvasir.research.corpus import Corpus
from kvasir.research.fetch import PIECE_CHARACTERS, FetchError, Page, fetch_page, split
from kvasir.research.ground import grounded
from kvasir.research.signatures import Intake
from kvasir.research.text import misleading, quoted, with_table_header
from kvasir.runs.progress import RESEARCH, ProgressStream
from kvasir.storm import runtime
from kvasir.storm.encoder import Encoder, cosine_similarity
from kvasir.storm.lm import GatewayModel

# How much of one page the embedding filter keeps, the only part a model reads: the 3 pieces of
# 6,000 characters it kept before pages were split at their headings, whose pieces are smaller.
PAGE_CHARACTERS = 3 * PIECE_CHARACTERS
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
    corpus: Corpus,
    checker: GatewayModel | None = None,
    attempt: int | None = None,
    stage: str = RESEARCH,
) -> list[Claim]:
    """The claims in the pieces of each page closest to `question`, the one it was found for.

    Kept by the question rather than the brief: a page found for one narrow fact holds it in one
    piece, which the brief would not rank first. Each read is published under the event `attempt`,
    the one that began the search. Every piece of a page read goes into `corpus`, and those read
    are marked.
    """
    pages = []
    for hit, fetched in zip(hits, fetch_all(hits), strict=True):
        if isinstance(fetched, str):
            pages.append((hit, split(fetched), True))
            continue
        # A lead's title is the anchor that linked it, which the page's own replaces.
        if "depth" in hit and fetched.title:
            hit = {**hit, "title": fetched.title}
        parts = relevant_pieces(
            corpus, encoder, question, hit["title"], fetched, hit.get("depth", 0) + 1
        )
        pages.append((hit, parts, False))
    size = page_characters(runtime.context_tokens())
    texts = [
        (page, hit, text, from_snippet)
        for page, (hit, parts, from_snippet) in enumerate(pages, 1)
        for text in pack(parts, size)
    ]

    def publish(i: int, done: int) -> None:
        page, hit, *_ = texts[i]
        stream.publish(
            stage,
            f"{label}: read page {page}/{len(pages)}: {hit['title'] or hit['url']}",
            done,
            len(texts),
            kind="part" if sum(p == page for p, *_ in texts) > 1 else "page",
            parent=attempt,
            page=page,
        )

    return take_all(
        model,
        checker,
        brief,
        question,
        [(hit, text, from_snippet) for _, hit, text, from_snippet in texts],
        first_id,
        step,
        publish,
    )


def recall(
    model: GatewayModel,
    encoder: Encoder,
    brief: str,
    question: str,
    corpus: Corpus,
    first_id: int,
    step: int,
    label: str,
    stream: ProgressStream,
    checker: GatewayModel | None = None,
    *,
    question_id: int | None = None,
    parent: int | None = None,
    stage: str = RESEARCH,
    claims_held: int | None = None,
) -> list[Claim]:
    """The claims in the unread pieces of `corpus` closest to `question`, read with no search.

    The pieces of a page are read together, as `read` reads them, and are marked read by the
    recall that returns them, so a question never reads the same piece twice. When it finds
    pieces it publishes a `recall` event under `parent`, a `part` event under it for each text
    read, and a `result` event under it with the claims added and the total, counted from
    `claims_held` (by default `first_id - 1`); when it finds none, nothing.
    """
    pages: dict[str, tuple[Page, list[str]]] = {}
    for found, text in corpus.recall(encoder, question):
        pages.setdefault(found.url, (found, []))[1].append(text)
    if not pages:
        return []
    size = page_characters(runtime.context_tokens())
    texts = [
        # The URL the page answered at: a page from the cache has its normalized one as `url`.
        ({"url": found.final_url, "title": found.title}, text, False)
        for found, parts in pages.values()
        for text in pack(parts, size)
    ]
    # The page each text came from, counted from 1 in the order the recall read them.
    numbers = [
        number for number, (_, parts) in enumerate(pages.values(), 1) for _ in pack(parts, size)
    ]
    held = sum(len(parts) for _, parts in pages.values())
    recalled = stream.publish(
        stage,
        f"{label}: {held} pieces from {len(pages)} pages",
        held,
        len(pages),
        kind="recall",
        parent=parent,
        question=question_id,
    )

    def publish(i: int, done: int) -> None:
        hit = texts[i][0]
        stream.publish(
            stage,
            f"{label}: read page {numbers[i]}/{len(pages)}: {hit['title'] or hit['url']}",
            done,
            len(texts),
            kind="part",
            parent=recalled,
            page=numbers[i],
        )

    new = take_all(model, checker, brief, question, texts, first_id, step, publish)
    corpus.record(new)
    total = (first_id - 1 if claims_held is None else claims_held) + len(new)
    stream.publish(
        stage,
        f"{label}: {len(new)} new claims, {total} in all",
        len(new),
        total,
        kind="result",
        parent=recalled,
    )
    return new


def take_all(
    model: GatewayModel,
    checker: GatewayModel | None,
    brief: str,
    question: str,
    texts: list[tuple[dict[str, Any], str, bool]],
    first_id: int,
    step: int,
    publish: Callable[[int, int], None],
) -> list[Claim]:
    """The claims in each of `texts`, a hit, a text of its page and whether it is a snippet,
    numbered from `first_id` in order. `publish` is told each read as it finishes."""

    def take(call: tuple[dict[str, Any], str, bool]) -> list[Claim]:
        hit, text, from_snippet = call
        return intake(model, brief, question, hit, text, 0, step, from_snippet)

    taken: list[tuple[str, list[Claim]]] = []
    # Intake is most of a run's time, so its calls run side by side, as wide as the pools, and each
    # is its own event: the page read is what a follower wants to see.
    with runtime.ContextThreadPoolExecutor(max_workers=runtime.max_threads()) as pool:
        for i, found in enumerate(pool.map(take, texts)):
            publish(i, i + 1)
            taken.append((texts[i][1], found))
    new: list[Claim] = []
    # Grounded once every page is read, not after each: the checker and the intake model share one
    # local server, and alternating between them would load a model for each page.
    for text, found in taken:
        # Numbered in page order once read, however the calls finished.
        for claim in found if checker is None else supported(checker, text, found):
            claim.id = first_id + len(new)
            new.append(claim)
    return new


def fetch_all(hits: list[dict[str, Any]]) -> Iterator[Page | str]:
    """Each page, or its search snippet when the page cannot be read. A lead has no snippet: its
    description is what another page says of it, so one that cannot be read gives no text."""

    def fetch(hit: dict[str, Any]) -> Page | str:
        try:
            return fetch_page(hit["url"])
        except FetchError as exc:
            logger.info("falling back to the snippet: %s", exc)
            return "" if "depth" in hit else str(hit["description"])

    with runtime.ContextThreadPoolExecutor(max_workers=runtime.max_threads()) as pool:
        yield from pool.map(fetch, hits)


def relevant_pieces(
    corpus: Corpus, encoder: Encoder, query: str, title: str, page: Page, lead_depth: int = 1
) -> list[str]:
    """The pieces of `page` closest to `query`, in page order, as many as `PAGE_CHARACTERS` holds.
    All of them go into `corpus`, encoded once, each with the page's title, which the piece itself
    may not name; a model reads the piece alone. The ones returned are marked read. The links of
    the page become leads of depth `lead_depth`."""
    pieces = split(page.text)
    titled = [f"{title}\n{piece}" for piece in pieces]
    added = corpus.add(page, pieces, np.asarray(encoder.encode(titled), dtype=float))
    corpus.add_leads(encoder, page, lead_depth)
    kept = list(range(len(pieces)))
    if sum(map(len, pieces)) > PAGE_CHARACTERS:
        kept = []
        length = 0
        scores = cosine_similarity(
            encoder.encode([query], query=True), np.stack([piece.vector for piece in added])
        )[0]
        for i in (int(i) for i in np.argsort(-scores, kind="stable")):
            if length + len(pieces[i]) > PAGE_CHARACTERS:
                continue
            kept.append(i)
            length += len(pieces[i])
    for i in kept:
        added[i].read = True
    return [pieces[i] for i in sorted(kept)]


def intake(
    model: GatewayModel,
    brief: str,
    question: str,
    hit: dict[str, Any],
    piece: str,
    first_id: int,
    step: int,
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
    except (AdapterParseError, ValidationError) as exc:
        logger.warning("could not read the claims from %s: %s", hit["url"], exc)
        return []
    claims: list[Claim] = []
    for finding in findings:
        if not quoted(finding.quote, piece):
            logger.info("dropped a claim whose quote is not on %s: %r", hit["url"], finding.quote)
            continue
        if misleading(finding.quote, source(finding.quote, piece)):
            logger.info("dropped a claim whose quote leaves out a negation: %r", finding.quote)
            continue
        claims.append(
            Claim(
                id=first_id + len(claims),
                text=finding.claim,
                # After the checks: the header's ellipsis would make `misleading` read the rows
                # between.
                quote=with_table_header(finding.quote, piece),
                url=hit["url"],
                title=hit["title"],
                confidence=finding.confidence,
                step=step,
                from_snippet=from_snippet,
            )
        )
    return claims


def supported(checker: GatewayModel, piece: str, claims: list[Claim]) -> list[Claim]:
    """Those of `claims`, read from `piece`, that the piece their quote is in supports."""
    kept = []
    for claim in claims:
        if grounded(checker, source(claim.quote, piece), claim.text):
            kept.append(claim)
        else:
            logger.info("dropped a claim %s does not support: %r", claim.url, claim.text)
    return kept


def source(quote: str, piece: str) -> str:
    """The part of `piece` that `quote` is in, which is all a small checker has room for."""
    return next((part for part in piece.split(GAP) if quoted(quote, part)), piece)
