"""The evidence a run gathers, and lookups over it by meaning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from kvasir.storm.encoder import Encoder, cosine_similarity

Confidence = Literal["high", "medium", "low"]


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
    # Claims from other sites that support this one, and that disagree with it.
    corroborated_by: list[int] = field(default_factory=list)
    contradicted_by: list[int] = field(default_factory=list)


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


def listed(claims: list[Claim]) -> str:
    return "\n".join(f"[{claim.id}] {claim.text}" for claim in claims)


def quoted_claims(claims: list[Claim]) -> str:
    return "\n".join(f"[{claim.id}] {claim.text}\n    quote: {claim.quote}" for claim in claims)
