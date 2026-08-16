"""Nested fan-out stays inside one process-wide budget."""

from __future__ import annotations

import threading

import pytest

from kvasir.storm import runtime
from kvasir.storm.utils import WebPageHelper


@pytest.fixture
def budget():
    """Configure a small budget, and put the default back afterwards."""
    runtime.configure_concurrency(3)
    yield 3
    runtime.configure_concurrency(runtime.DEFAULT_MAX_THREADS)


class _Counter:
    """Records the high-water mark of concurrent calls."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def enter(self) -> None:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def leave(self) -> None:
        with self.lock:
            self.now -= 1


def test_nested_retrieval_never_exceeds_the_budget(budget):
    """The three pools nest, so upstream's worst case was the product of their widths."""
    from kvasir.storm.interface import Retriever

    counter = _Counter()
    helper = WebPageHelper(min_char_count=1)

    def download(url):
        counter.enter()
        try:
            # Long enough that a violation is a certainty rather than a race.
            threading.Event().wait(0.02)
            return b"<html><body>" + b"word " * 100 + b"</body></html>"
        finally:
            counter.leave()

    helper.download_webpage = download

    class _RM:
        def __call__(self, query_or_queries, exclude_urls):
            (query,) = query_or_queries
            urls = [f"https://example.invalid/{query}/{n}" for n in range(6)]
            articles = helper.urls_to_articles(urls)
            return [
                {"url": url, "title": query, "description": "", "snippets": ["x"]}
                for url in articles
            ]

    # A wider retrieval pool than the budget, so the budget is what has to bind.
    Retriever(rm=_RM(), max_thread=8).retrieve([f"q{n}" for n in range(8)])

    assert counter.peak <= budget
    # And the budget is actually being spent, so the bound is not passing by doing nothing.
    assert counter.peak == budget


def test_the_page_pool_defaults_to_the_configured_width(budget):
    assert WebPageHelper().max_thread_num == budget
    assert WebPageHelper(max_thread_num=1).max_thread_num == 1


def test_every_permit_comes_back(budget):
    helper = WebPageHelper(min_char_count=1)
    helper.download_webpage = lambda url: None

    helper.urls_to_articles([f"https://example.invalid/{n}" for n in range(10)])

    # A leaked permit shrinks the budget for every later run, so this is the failure that would
    # not show up until a second run went quiet.
    for _ in range(budget):
        assert runtime._fetch_slots.acquire(blocking=False)
    for _ in range(budget):
        runtime.release_fetch_slot()


def test_a_failing_download_releases_its_permit(budget):
    helper = WebPageHelper(min_char_count=1)

    def download(url):
        raise RuntimeError("connection reset")

    helper.download_webpage = download

    with pytest.raises(RuntimeError, match="connection reset"):
        helper.urls_to_articles(["https://example.invalid/1"])

    for _ in range(budget):
        assert runtime._fetch_slots.acquire(blocking=False)
    for _ in range(budget):
        runtime.release_fetch_slot()
