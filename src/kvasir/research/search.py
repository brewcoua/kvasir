"""Searching, skipping what the run already read, and choosing which results to read.

Each query's results are ranked three ways before any page is downloaded: by SearXNG's score, by
closeness of the title and snippet to the question, and by distance from what the run holds. The
rankings are fused with weighted reciprocal rank fusion (Cormack, Clarke and Buttcher, SIGIR 2009),
then picked greedily, skipping a repeated domain or a near-duplicate snippet, as in maximal marginal
relevance (Carbonell and Goldstein, 1998). Without an encoder only SearXNG's score and the domain
skip apply.

The weights are not tuned. They are a first guess, to be kept or changed on evaluation.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from kvasir.research.corroborate import domain
from kvasir.storm import runtime
from kvasir.storm.encoder import Encoder, cosine_similarity
from kvasir.storm.rm import SearXNG

# Results of one query that are ranked; the rest are dropped.
POOL = 20
# Reciprocal rank fusion's constant, 60 in the paper: how flat the reward for a high rank is.
RRF_K = 60
# What each ranking adds to the fused score.
SEARXNG_WEIGHT = 1.0
RELEVANCE_WEIGHT = 1.0
NOVELTY_WEIGHT = 0.5
# Above this cosine similarity, a snippet repeats one already picked for the query.
NEAR_DUPLICATE = 0.9


class SearchUnavailable(RuntimeError):
    """Searches kept failing, so the run stops rather than write from no evidence."""

    def __init__(self, searches: int, failures: list[str]) -> None:
        self.failures = list(dict.fromkeys(failures))
        super().__init__(f"{searches} searches in a row failed: {'; '.join(self.failures)}")


def search(
    searxng: SearXNG,
    queries: list[str],
    per_query: int,
    seen: set[str],
    encoder: Encoder | None = None,
    question: str = "",
    held: np.ndarray | None = None,
) -> list[dict[str, Any]]:
    """Results not read before, at most `per_query` from each query. Marks them seen.

    The results are the best by SearXNG's score, fused with their closeness to `question` when
    there is an `encoder`, and with their distance from the vectors in `held` when there are any.
    At most one result per domain is taken, and none that repeats another's snippet; the next
    ranked takes a skipped one's place. Skipped results are not marked seen.
    """
    hits = []
    failures: list[str] = []
    for query in queries:
        runtime.record_search_usage("SearXNG", 1)
        found = searxng(query, exclude_urls=list(seen))
        failures += searxng.failures
        pool = sorted(
            (hit for hit in found if hit["url"] not in seen),
            key=lambda hit: hit.get("score", 0.0),
            reverse=True,
        )[:POOL]
        if not pool:
            continue
        fused = [SEARXNG_WEIGHT / (RRF_K + rank) for rank in range(1, len(pool) + 1)]
        vectors = None
        if encoder is not None:
            vectors = np.asarray(
                encoder.encode([f"{hit['title']}\n{hit['description']}" for hit in pool]),
                dtype=float,
            )
            wanted = np.asarray(encoder.encode([question], query=True), dtype=float)
            rankings = [(RELEVANCE_WEIGHT, cosine_similarity(wanted, vectors)[0])]
            if held is not None and len(held):
                rankings.append(
                    (NOVELTY_WEIGHT, 1.0 - cosine_similarity(vectors, held).max(axis=1))
                )
            for weight, scores in rankings:
                # A stable sort, so equal scores keep SearXNG's order.
                for rank, i in enumerate(np.argsort(-scores, kind="stable"), start=1):
                    fused[i] += weight / (RRF_K + rank)
        picked: list[int] = []
        domains: set[str] = set()
        for i in sorted(range(len(pool)), key=lambda i: -fused[i]):
            if len(picked) == per_query:
                break
            if domain(pool[i]["url"]) in domains:
                continue
            if vectors is not None and any(
                cosine_similarity(vectors[i], vectors[j])[0][0] > NEAR_DUPLICATE for j in picked
            ):
                continue
            picked.append(i)
            domains.add(domain(pool[i]["url"]))
            seen.add(pool[i]["url"])
            hits.append(pool[i])
    if not hits and failures:
        raise SearchUnavailable(len(queries), failures)
    return hits
