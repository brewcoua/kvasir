"""The evidence a run gathers, and lookups over it by meaning."""

from __future__ import annotations

from dataclasses import dataclass
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
    round: int


def closest(encoder: Encoder, query: str, texts: list[str], k: int) -> list[int]:
    """Indices of the `k` texts closest to `query`, in their original order."""
    if len(texts) <= k:
        return list(range(len(texts)))
    scores = cosine_similarity(encoder.encode([query]), encoder.encode(texts))[0]
    return sorted(int(i) for i in np.argsort(scores)[::-1][:k])
