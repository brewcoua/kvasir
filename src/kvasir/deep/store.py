"""The evidence a run gathers, and lookups over it by meaning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from kvasir.storm.encoder import Encoder, cosine_similarity

Confidence = Literal["high", "medium", "low"]
# `open`: nothing found yet. `weak`: part of an answer. `contradicted`: sources disagree.
# `unresolved`: every query shape was tried and found nothing.
Status = Literal["open", "weak", "answered", "contradicted", "unresolved"]


@dataclass
class Claim:
    """One fact read from one page, with the quote that supports it."""

    id: int
    text: str
    quote: str
    url: str
    title: str
    confidence: Confidence
    # The step of the research that found it; 0 for the background searches.
    step: int


@dataclass
class Question:
    """One thing the research has to find out, and how far it got."""

    id: int
    text: str
    # The question this one follows up on, one level shallower.
    parent: int | None = None
    depth: int = 0
    status: Status = "open"
    # The claims whose quotes support the answer, as the last judgement found them.
    claims: list[int] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    # What the last judgement found missing, for the next query to look for.
    missing: str = ""
    # How many claims the store held at the last judgement, so a question is judged again only
    # once there is something new to judge it on.
    judged_at: int = 0
    # For a ranking or a table: the option and the criterion of the empty cell this asks about.
    cell: tuple[str, str] | None = None

    @property
    def attempts(self) -> int:
        return len(self.queries)


def closest(encoder: Encoder, query: str, texts: list[str], k: int) -> list[int]:
    """Indices of the `k` texts closest to `query`, in their original order."""
    if len(texts) <= k:
        return list(range(len(texts)))
    return nearest(encoder, query, encoder.encode(texts), k)


def nearest(encoder: Encoder, query: str, vectors: np.ndarray, k: int) -> list[int]:
    """`closest`, over texts already encoded, for when many queries search the same texts."""
    if len(vectors) <= k:
        return list(range(len(vectors)))
    scores = cosine_similarity(encoder.encode([query]), vectors)[0]
    return sorted(int(i) for i in np.argsort(scores)[::-1][:k])
