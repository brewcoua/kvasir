"""Fan-out stays inside one process-wide budget."""

from __future__ import annotations

import threading

import pytest

from kvasir.storm import runtime
from kvasir.storm.interface import Retriever
from kvasir.storm.rm import SearXNG


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


class _Response:
    def json(self):
        return {"results": []}


def _searxng(monkeypatch, on_request):
    """A SearXNG retriever whose outbound request is `on_request` rather than a real one."""
    monkeypatch.setattr(
        "kvasir.storm.rm.httpx.get",
        lambda url, headers=None, params=None, timeout=None: on_request(),
    )
    return SearXNG(searxng_api_url="http://searxng.invalid")


def test_retrieval_never_exceeds_the_budget(budget, monkeypatch):
    """Retrieval is driven by a pool above it, so its width alone does not bound the requests."""
    counter = _Counter()

    def request():
        counter.enter()
        try:
            # Long enough that a violation is a certainty rather than a race.
            threading.Event().wait(0.02)
            return _Response()
        finally:
            counter.leave()

    # A wider retrieval pool than the budget, so the budget is what has to bind.
    retriever = Retriever(rm=_searxng(monkeypatch, request), max_thread=8)
    retriever.retrieve([f"q{n}" for n in range(8)])

    assert counter.peak <= budget
    # And the budget is actually being spent, so the bound is not passing by doing nothing.
    assert counter.peak == budget


def test_every_permit_comes_back(budget, monkeypatch):
    retriever = Retriever(rm=_searxng(monkeypatch, _Response), max_thread=8)
    retriever.retrieve([f"q{n}" for n in range(10)])

    # A leaked permit shrinks the budget for every later run, so this is the failure that would not
    # show up until a second run went quiet.
    for _ in range(budget):
        assert runtime._fetch_slots.acquire(blocking=False)
    for _ in range(budget):
        runtime._fetch_slots.release()


def test_a_failing_request_releases_its_permit(budget, monkeypatch):
    def request():
        raise RuntimeError("connection reset")

    retriever = Retriever(rm=_searxng(monkeypatch, request), max_thread=8)
    # SearXNG logs a failed query and returns what it has, so this raises nothing.
    retriever.retrieve(["q0", "q1"])

    for _ in range(budget):
        assert runtime._fetch_slots.acquire(blocking=False)
    for _ in range(budget):
        runtime._fetch_slots.release()


def test_the_configured_width_reaches_the_pipeline(budget):
    assert runtime.max_threads() == budget
