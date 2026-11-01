"""Searching, skipping what the run already read."""

from __future__ import annotations

from typing import Any

from kvasir.storm import runtime
from kvasir.storm.rm import SearXNG


class SearchUnavailable(RuntimeError):
    """Searches kept failing, so the run stops rather than write from no evidence."""

    def __init__(self, searches: int, failures: list[str]) -> None:
        self.failures = list(dict.fromkeys(failures))
        super().__init__(f"{searches} searches in a row failed: {'; '.join(self.failures)}")


def search(
    searxng: SearXNG, queries: list[str], per_query: int, seen: set[str]
) -> list[dict[str, Any]]:
    """Results not read before, at most `per_query` from each query. Marks them seen."""
    hits = []
    failures: list[str] = []
    for query in queries:
        runtime.record_search_usage("SearXNG", 1)
        found = [hit for hit in searxng(query, exclude_urls=list(seen)) if hit["url"] not in seen]
        failures += searxng.failures
        for hit in found[:per_query]:
            seen.add(hit["url"])
            hits.append(hit)
    if not hits and failures:
        raise SearchUnavailable(len(queries), failures)
    return hits
