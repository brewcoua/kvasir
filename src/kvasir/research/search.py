"""Searching, skipping what the run already read, and choosing which results to read.

Each query's results are ranked four ways before any page is downloaded: by SearXNG's score, by
closeness of the title and snippet to the question, by distance from what the run holds, and by
the trust tier of the site. A result of an `avoid` site is dropped before ranking. The rankings
are fused with weighted reciprocal rank fusion (Cormack, Clarke and Buttcher, SIGIR 2009), then
picked greedily, skipping a repeated domain or a near-duplicate snippet, as in maximal marginal
relevance (Carbonell and Goldstein, 1998). Without an encoder only SearXNG's score and the domain
skip apply; without a `sources` classifier there is no tier ranking and no drop.

A run's leads, the links of pages it read, join each query's results as candidates, at most
LEADS_PER_QUERY of them picked. Their worth stands in for SearXNG's score, which a lead has not. A
lead is picked only if its site's robots.txt allows it; a result is fetched as it is. No hit is
picked from a site the run has read its limit of pages from.

The weights are not tuned. They are a first guess, to be kept or changed on evaluation.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import numpy as np

from kvasir.research import fetch
from kvasir.research.corroborate import domain
from kvasir.storm import runtime
from kvasir.storm.encoder import Encoder, cosine_similarity
from kvasir.storm.rm import SearXNG

if TYPE_CHECKING:
    from kvasir.research.claims import Claim
    from kvasir.research.corpus import Corpus
    from kvasir.research.sources import Sources

# Results of one query that are ranked; the rest are dropped.
POOL = 20
# Engines read the first few dozen words of a query and ignore the rest, so a longer one only adds
# noise; a planner that degenerated once wrote thousands of characters of quoted terms.
MAX_QUERY_CHARACTERS = 300
# Reciprocal rank fusion's constant, 60 in the paper: how flat the reward for a high rank is.
RRF_K = 60
# What each ranking adds to the fused score.
SEARXNG_WEIGHT = 1.0
RELEVANCE_WEIGHT = 1.0
NOVELTY_WEIGHT = 0.5
TRUST_WEIGHT = 0.5
# The tiers in the order they rank, best first; `avoid` never reaches the ranking.
TRUST_ORDER = ("high", "normal", "unknown", "low")
# Above this cosine similarity, a snippet repeats one already picked for the query.
NEAR_DUPLICATE = 0.9
# Leads picked for one query, however many are worth reading.
LEADS_PER_QUERY = 2

logger = logging.getLogger(__name__)

# What a cut query must not end on: a boolean operator, or a prefix operator whose term was cut.
_DANGLING = re.compile(r"\s*(?:\b(?:OR|AND|NOT)|[-+!|])\s*$")


def _cap(query: str) -> str:
    """`query` cut to `MAX_QUERY_CHARACTERS` at a word boundary, without a half-quoted term or an
    operator left hanging by the cut."""
    if len(query) <= MAX_QUERY_CHARACTERS:
        return query
    cut = query[:MAX_QUERY_CHARACTERS]
    space = cut.rfind(" ")
    if not query[MAX_QUERY_CHARACTERS].isspace() and space > 0:
        cut = cut[:space]
    if cut.count('"') % 2:
        cut = cut[: cut.rindex('"')]
    cut = cut.rstrip()
    while match := _DANGLING.search(cut):
        cut = cut[: match.start()]
    logger.info("search query cut from %d to %d characters", len(query), len(cut))
    return cut


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
    *,
    corpus: Corpus | None = None,
    claims: list[Claim] | None = None,
    sources: Sources | None = None,
) -> list[dict[str, Any]]:
    """Results not read before, at most `per_query` from each query. Marks them seen.

    The results are the best by SearXNG's score, fused with their closeness to `question` when
    there is an `encoder`, and with their distance from the vectors in `held` when there are any.
    At most one result per domain is taken, and none that repeats another's snippet; the next
    ranked takes a skipped one's place. Skipped results are not marked seen.

    With a `corpus` and an `encoder`, its leads for `question` join the results, as many as
    `LEADS_PER_QUERY` picked. `claims` are what the leads' site affinity is counted from.

    With `sources`, the hosts of all the queries' results and leads are classified in one batch,
    results of an `avoid` site are dropped, and marked seen, even when nothing else is left, and
    the tier is a fourth ranking when the pool's tiers differ.
    """
    hits = []
    failures: list[str] = []
    gathered = []
    for query in queries:
        runtime.record_search_usage("SearXNG", 1)
        found = searxng(_cap(query), exclude_urls=list(seen))
        failures += searxng.failures
        results = sorted(
            (hit for hit in found if hit["url"] not in seen),
            key=lambda hit: hit.get("score", 0.0),
            reverse=True,
        )[:POOL]
        leads = (
            corpus.leads_for(encoder, question, held, claims or [], seen, POOL)
            if corpus is not None and encoder is not None
            else []
        )
        gathered.append((results, leads))
    tiers: dict[str, str] = {}
    if sources is not None:
        tiers = {
            host: source.tier
            for host, source in sources.classify(
                {domain(hit["url"]) for results, leads in gathered for hit in [*results, *leads]}
            ).items()
        }
        seen.update(
            hit["url"]
            for results, leads in gathered
            for hit in [*results, *leads]
            if tiers[domain(hit["url"])] == "avoid"
        )
    for results, leads in gathered:
        # An earlier query may have picked a URL, or it may be an `avoid` one.
        results = [hit for hit in results if hit["url"] not in seen]
        leads = [hit for hit in leads if hit["url"] not in seen]
        pool = [*results, *leads]
        if not pool:
            continue
        # Results by SearXNG's score, leads by their worth.
        fused = [
            SEARXNG_WEIGHT / (RRF_K + rank)
            for ranks in (len(results), len(leads))
            for rank in range(1, ranks + 1)
        ]
        vectors = None
        rankings: list[tuple[float, np.ndarray]] = []
        if encoder is not None:
            vectors = np.asarray(
                encoder.encode([f"{hit['title']}\n{hit['description']}" for hit in pool]),
                dtype=float,
            )
            wanted = np.asarray(encoder.encode([question], query=True), dtype=float)
            rankings.append((RELEVANCE_WEIGHT, cosine_similarity(wanted, vectors)[0]))
            if held is not None and len(held):
                rankings.append(
                    (NOVELTY_WEIGHT, 1.0 - cosine_similarity(vectors, held).max(axis=1))
                )
        trust = [-TRUST_ORDER.index(tiers[domain(hit["url"])]) for hit in pool] if tiers else []
        if len(set(trust)) > 1:
            rankings.append((TRUST_WEIGHT, np.asarray(trust, dtype=float)))
        for weight, scores in rankings:
            # A stable sort, so equal scores keep SearXNG's order.
            for rank, i in enumerate(np.argsort(-scores, kind="stable"), start=1):
                fused[i] += weight / (RRF_K + rank)
        picked: list[int] = []
        domains: set[str] = set()
        followed = 0
        for i in sorted(range(len(pool)), key=lambda i: -fused[i]):
            if len(picked) == per_query:
                break
            is_lead = i >= len(results)
            if domain(pool[i]["url"]) in domains or (
                corpus is not None and corpus.full(pool[i]["url"])
            ):
                continue
            if is_lead and followed == LEADS_PER_QUERY:
                continue
            if vectors is not None and any(
                cosine_similarity(vectors[i], vectors[j])[0][0] > NEAR_DUPLICATE for j in picked
            ):
                continue
            if corpus is not None and is_lead and not fetch.allowed(pool[i]["url"]):
                corpus.refuse(pool[i]["url"])
                continue
            picked.append(i)
            followed += is_lead
            domains.add(domain(pool[i]["url"]))
            seen.add(pool[i]["url"])
            hits.append(pool[i])
    if not hits and failures:
        raise SearchUnavailable(len(queries), failures)
    return hits
