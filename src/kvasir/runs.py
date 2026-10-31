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
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from kvasir import logs
from kvasir.models import (
    ModelUsage,
    Progress,
    RunDetail,
    RunSpan,
    RunStage,
    RunStatus,
    RunSummary,
    RunUsage,
    StageUsage,
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

# Where usage lands when it arrives before the first progress event names a stage.
UNSTAGED = "unstaged"

_current: ContextVar[Run | None] = ContextVar("current_run", default=None)

# Ends a watcher's stream. Distinct from None, which a watcher with a heartbeat yields when idle.
_END: Any = object()


class RunNotFound(Exception):
    """No run with that id. Either it never existed or it aged out of the registry."""


@dataclass
class _Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    embedding_tokens: int = 0
    searches: int = 0

    def add(
        self, prompt_tokens: int, completion_tokens: int, reasoning_tokens: int, cost: float
    ) -> None:
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.reasoning_tokens += reasoning_tokens
        self.cost += cost

    def model(self) -> ModelUsage:
        return ModelUsage(
            calls=self.calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            reasoning_tokens=self.reasoning_tokens,
            cost=round(self.cost, 6),
        )

    def stage(self) -> StageUsage:
        return StageUsage(
            **self.model().model_dump(),
            embedding_tokens=self.embedding_tokens,
            searches=self.searches,
        )


@dataclass
class _Stage:
    spans: list[RunSpan] = field(default_factory=list)

    def enter(self, now: float) -> None:
        self.spans.append(RunSpan(started_at=now, ended_at=None))

    def leave(self, now: float) -> None:
        if self.spans[-1].ended_at is None:
            self.spans[-1].ended_at = now

    def model(self, name: str, now: float) -> RunStage:
        return RunStage(
            name=name,
            started_at=self.spans[0].started_at,
            ended_at=self.spans[-1].ended_at,
            seconds=round(sum((span.ended_at or now) - span.started_at for span in self.spans), 1),
            spans=[span.model_copy() for span in self.spans],
        )


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
    planned_stages: list[str] = field(default_factory=list)
    result: BaseModel | None = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stages: dict[str, _Stage] = field(default_factory=dict, repr=False)
    _events: deque[Progress] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS), repr=False)
    _by_model: dict[str, _Usage] = field(default_factory=dict, repr=False)
    _by_role: dict[str, _Usage] = field(default_factory=dict, repr=False)
    _by_stage: dict[str, _Usage] = field(default_factory=dict, repr=False)
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

    def set_result(self, result: BaseModel) -> None:
        with self._lock:
            self.result = result

    def _finish(self, state: str, error: str | None) -> None:
        with self._lock:
            self.state = state
            self.error = error
            self.ended_at = time.time()
            if self.stage is not None:
                self._stages[self.stage].leave(self.ended_at)
        self._notify(_END)

    def record_progress(self, event: Progress) -> None:
        with self._lock:
            if event.stage != self.stage:
                if self.stage is not None:
                    self._stages[self.stage].leave(event.at)
                # Re-entering a stage adds a span to the one already there: Co-STORM alternates
                # between two stages for the length of a session.
                self._stages.setdefault(event.stage, _Stage()).enter(event.at)
                self.stage = event.stage
            self._events.append(event)
        self._notify(event)

    def record_lm(
        self,
        model: str,
        role: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
        reasoning_tokens: int = 0,
    ) -> None:
        with self._lock:
            # Attributed to the stage the run is in now. Co-STORM can switch stages while a call is
            # in flight, so its split is close rather than exact.
            for usage in (
                self._by_model.setdefault(model, _Usage()),
                self._by_role.setdefault(role or "unattributed", _Usage()),
                self._stage_usage(),
            ):
                usage.add(prompt_tokens, completion_tokens, reasoning_tokens, cost)

    def record_embedding(self, model: str, tokens: int) -> None:
        with self._lock:
            self._embedding_tokens += tokens
            self._stage_usage().embedding_tokens += tokens

    def record_search(self, engine: str, queries: int) -> None:
        with self._lock:
            self._searches += queries
            self._stage_usage().searches += queries

    def _stage_usage(self) -> _Usage:
        return self._by_stage.setdefault(self.stage or UNSTAGED, _Usage())

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
                planned_stages=self.planned_stages,
                latest=self._events[-1] if self._events else None,
            )

    def status(self) -> RunStatus:
        summary = self.summary()
        with self._lock:
            now = self.ended_at or time.time()
            return RunStatus(
                **dict(summary),
                stages=[stage.model(name, now) for name, stage in self._stages.items()],
            )

    def detail(self) -> RunDetail:
        status = self.status()
        with self._lock:
            return RunDetail(**dict(status), events=list(self._events), result=self.result)

    def _usage(self) -> RunUsage:
        return RunUsage(
            prompt_tokens=sum(usage.prompt_tokens for usage in self._by_model.values()),
            completion_tokens=sum(usage.completion_tokens for usage in self._by_model.values()),
            reasoning_tokens=sum(usage.reasoning_tokens for usage in self._by_model.values()),
            cost=round(sum(usage.cost for usage in self._by_model.values()), 6),
            embedding_tokens=self._embedding_tokens,
            searches=self._searches,
            by_model={name: usage.model() for name, usage in self._by_model.items()},
            by_role={name: usage.model() for name, usage in self._by_role.items()},
            by_stage={name: usage.stage() for name, usage in self._by_stage.items()},
        )

    def watch(self, heartbeat: float | None = None) -> _Watcher:
        """Follow this run live. Give the watcher back with `unwatch` when done.

        With a heartbeat, the watcher also yields None after that many idle seconds, so a follower
        can refresh what changes between events, such as usage during a long silent stage.
        """
        watcher = _Watcher(heartbeat)
        with self._lock:
            self._watchers.append(watcher)
        return watcher

    def unwatch(self, watcher: _Watcher) -> None:
        with self._lock:
            if watcher in self._watchers:
                self._watchers.remove(watcher)

    def _notify(self, event: Any) -> None:
        with self._lock:
            watchers = list(self._watchers)
        for watcher in watchers:
            watcher.push(event)


class _Watcher:
    """One live follower of a run.

    Events arrive on pipeline threads and are read from the event loop, so each watcher remembers
    the loop it was created on and hands the event over through it.
    """

    def __init__(self, heartbeat: float | None) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._heartbeat = heartbeat

    def push(self, event: Any) -> None:
        # `_END` means the run reached a terminal state, which ends the stream.
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def __aiter__(self) -> AsyncIterator[Progress | None]:
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), self._heartbeat)
            except TimeoutError:
                yield None
                continue
            if event is _END:
                return
            yield event


class RunRegistry:
    """The most recent runs, newest first. Older ones fall off the end."""

    def __init__(self, limit: int = MAX_RUNS) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        self._limit = limit

    def start(self, kind: str, topic: str, planned_stages: list[str] | None = None) -> Run:
        run = Run(id=uuid4().hex[:12], kind=kind, topic=topic, planned_stages=planned_stages or [])
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


def record_progress(event: Progress) -> None:
    """Attribute a progress event to whichever run this thread is working on, if any."""
    run = _current.get()
    if run is not None:
        run.record_progress(event)
