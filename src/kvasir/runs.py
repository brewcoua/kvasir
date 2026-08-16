"""What the service is doing, and what it cost.

A run is minutes of work behind a single streaming response, so a client that disconnects, or one
that never started the run, has no way to find out how it is going. The registry is that record.

It lives in memory. Runs are observation, not state: nothing resumes from one, and a restart is
allowed to forget them. That is what keeps a database out of this.

Usage is reported by the pipeline itself, through `kvasir.storm.runtime`'s sink. A `Run` is the
sink, found through a contextvar rather than passed down, because the reporting happens deep inside
the fork and across every thread a run touches.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import uuid4

from kvasir import logs
from kvasir.models import (
    ModelUsage,
    Progress,
    RunDetail,
    RunStage,
    RunSummary,
    RunUsage,
)
from kvasir.storm import runtime

# Enough to see what happened over a working session, and bounded so a long-lived process cannot
# grow without limit. Neither is configuration worth exposing.
MAX_RUNS = 100
MAX_EVENTS = 200

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
REJECTED = "rejected"

_current: ContextVar[Run | None] = ContextVar("current_run", default=None)


class RunNotFound(Exception):
    """No run with that id. Either it never existed or it aged out of the registry."""


@dataclass
class _Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0

    def add(self, prompt_tokens: int, completion_tokens: int, cost: float) -> None:
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.cost += cost

    def model(self) -> ModelUsage:
        return ModelUsage(
            calls=self.calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cost=round(self.cost, 6),
        )


@dataclass
class _Stage:
    started_at: float
    ended_at: float | None = None


@dataclass
class Run:
    """One unit of work, and everything observed about it.

    Every mutating method takes the lock: the pipeline reports from its own thread pools, while
    the API reads from the event loop.
    """

    id: str
    kind: str
    topic: str
    state: str = QUEUED
    stage: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    error: str | None = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stages: dict[str, _Stage] = field(default_factory=dict, repr=False)
    _events: deque[Progress] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS), repr=False)
    _by_model: dict[str, _Usage] = field(default_factory=dict, repr=False)
    _by_role: dict[str, _Usage] = field(default_factory=dict, repr=False)
    _embedding_tokens: int = field(default=0, repr=False)
    _searches: int = field(default=0, repr=False)
    _watchers: list[_Watcher] = field(default_factory=list, repr=False)

    @contextmanager
    def active(self) -> Iterator[None]:
        """Mark the run running, and make it the one the pipeline reports into.

        Entered on the worker thread that does the work, so the contextvars it sets travel with
        that thread's pools rather than with the event loop.
        """
        with self._lock:
            self.state = RUNNING
            self.started_at = time.time()
        try:
            with (
                logs.run_context(self.id, self.kind),
                runtime.record_usage_into(self),
                _current_run(self),
            ):
                yield
        except BaseException as exc:
            self._finish(FAILED, f"{type(exc).__name__}: {exc}")
            raise
        else:
            self._finish(DONE, None)

    def reject(self) -> None:
        """Record a run that never got a slot, so saturation is visible rather than only a 429."""
        self._finish(REJECTED, "all run slots are busy")

    def _finish(self, state: str, error: str | None) -> None:
        with self._lock:
            self.state = state
            self.error = error
            self.ended_at = time.time()
            if self.stage is not None:
                self._stages[self.stage].ended_at = self.ended_at
        self._notify()

    def record_progress(self, stage: str, detail: str) -> None:
        with self._lock:
            if stage != self.stage:
                if self.stage is not None:
                    self._stages[self.stage].ended_at = time.time()
                # Re-entering a stage extends the one already there rather than starting a second:
                # Co-STORM alternates between two stages for the length of a session.
                self._stages.setdefault(stage, _Stage(started_at=time.time())).ended_at = None
                self.stage = stage
            event = Progress(stage=stage, detail=detail)
            self._events.append(event)
        self._notify(event)

    def record_lm(
        self, model: str, role: str | None, prompt_tokens: int, completion_tokens: int, cost: float
    ) -> None:
        with self._lock:
            self._by_model.setdefault(model, _Usage()).add(prompt_tokens, completion_tokens, cost)
            self._by_role.setdefault(role or "unattributed", _Usage()).add(
                prompt_tokens, completion_tokens, cost
            )

    def record_embedding(self, model: str, tokens: int) -> None:
        with self._lock:
            self._embedding_tokens += tokens

    def record_search(self, engine: str, queries: int) -> None:
        with self._lock:
            self._searches += queries

    def summary(self) -> RunSummary:
        with self._lock:
            return RunSummary(
                id=self.id,
                kind=self.kind,
                topic=self.topic,
                state=self.state,
                stage=self.stage,
                created_at=self.created_at,
                started_at=self.started_at,
                ended_at=self.ended_at,
                error=self.error,
                usage=self._usage(),
            )

    def detail(self) -> RunDetail:
        summary = self.summary()
        with self._lock:
            return RunDetail(
                **summary.model_dump(),
                stages=[
                    RunStage(name=name, started_at=stage.started_at, ended_at=stage.ended_at)
                    for name, stage in self._stages.items()
                ],
                events=list(self._events),
            )

    def _usage(self) -> RunUsage:
        return RunUsage(
            prompt_tokens=sum(usage.prompt_tokens for usage in self._by_model.values()),
            completion_tokens=sum(usage.completion_tokens for usage in self._by_model.values()),
            cost=round(sum(usage.cost for usage in self._by_model.values()), 6),
            embedding_tokens=self._embedding_tokens,
            searches=self._searches,
            by_model={name: usage.model() for name, usage in self._by_model.items()},
            by_role={name: usage.model() for name, usage in self._by_role.items()},
        )

    def watch(self) -> _Watcher:
        """Follow this run live. Give the watcher back with `unwatch` when done."""
        watcher = _Watcher()
        with self._lock:
            self._watchers.append(watcher)
        return watcher

    def unwatch(self, watcher: _Watcher) -> None:
        with self._lock:
            if watcher in self._watchers:
                self._watchers.remove(watcher)

    def _notify(self, event: Progress | None = None) -> None:
        with self._lock:
            watchers = list(self._watchers)
        for watcher in watchers:
            watcher.push(event)


class _Watcher:
    """One live follower of a run.

    Events arrive on pipeline threads and are read from the event loop, so each watcher remembers
    the loop it was created on and hands the event over through it.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Progress | None] = asyncio.Queue()

    def push(self, event: Progress | None) -> None:
        # None means the run reached a terminal state, which ends the stream.
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def __aiter__(self) -> AsyncIterator[Progress]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


class RunRegistry:
    """The most recent runs, newest first. Older ones fall off the end."""

    def __init__(self, limit: int = MAX_RUNS) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        self._limit = limit

    def start(self, kind: str, topic: str) -> Run:
        run = Run(id=uuid4().hex[:12], kind=kind, topic=topic)
        with self._lock:
            self._runs[run.id] = run
            while len(self._runs) > self._limit:
                # Insertion-ordered, so this drops the oldest.
                del self._runs[next(iter(self._runs))]
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(self) -> list[Run]:
        with self._lock:
            return list(reversed(self._runs.values()))


@contextmanager
def _current_run(run: Run) -> Iterator[None]:
    token = _current.set(run)
    try:
        yield
    finally:
        _current.reset(token)


def record_progress(stage: str, detail: str) -> None:
    """Attribute a progress event to whichever run this thread is working on, if any."""
    run = _current.get()
    if run is not None:
        run.record_progress(stage, detail)
