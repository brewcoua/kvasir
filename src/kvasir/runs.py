"""What the service is doing, and what it cost.

A run is minutes of work behind a single streaming response, so a client that disconnects, or one
that never started the run, has no way to find out how it is going. The registry is that record.

It lives in memory. Runs are observation, not state: nothing resumes from one, and by default a
restart forgets them. With KVASIR_RUN_STORE set to sqlite, each run is also written to a
`RunStore` when it starts and when it ends, which is history for debugging rather than state.

Usage is reported by the pipeline itself, through `kvasir.storm.runtime`'s sink. A `Run` is the
sink, found through a contextvar rather than passed down, because the reporting happens deep inside
the fork and across every thread a run touches.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from kvasir import logs
from kvasir.models import (
    LmCall,
    LmCallContent,
    ModelUsage,
    Progress,
    RunDetail,
    RunResult,
    RunSpan,
    RunStage,
    RunStatus,
    RunSummary,
    RunUsage,
    StageUsage,
)
from kvasir.storm import runtime

if TYPE_CHECKING:
    from kvasir.run_store import RunStore

# Enough for every event and call of a long research run, and bounded so a long-lived process
# cannot grow without limit. Neither is configuration worth exposing.
MAX_EVENTS = 1000
MAX_CALLS = 2000
# The calls a `status` carries. It is sent after every event, so it holds only the recent ones; a
# follower merges them into the detail's by id.
STATUS_CALLS = 50

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
REJECTED = "rejected"

# Where usage lands when it arrives before the first progress event names a stage.
UNSTAGED = "unstaged"

_current: ContextVar[Run | None] = ContextVar("current_run", default=None)
# The `seq` of the last event published in this line of work. The pools copy context when work is
# submitted, so a call made in a pool sees the event published before its work was handed out.
_event: ContextVar[int | None] = ContextVar("current_event", default=None)

# Ends a watcher's stream. Distinct from None, which a watcher with a heartbeat yields when idle.
_END: Any = object()


class RunNotFound(Exception):
    """No run with that id. Either it never existed or it aged out of retention."""


class CallContentNotFound(Exception):
    """No content for that call: keeping it is off, the call is unknown, or it aged out."""


@dataclass
class _Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    embedding_tokens: int = 0
    searches: int = 0
    seconds: float = 0.0

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
            seconds=round(self.seconds, 3),
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
    result: RunResult | None = None
    # Whether each call's messages and response are kept. See KVASIR_KEEP_CALL_CONTENT.
    keep_content: bool = False
    # Called once the run reaches a terminal state, outside the lock.
    on_end: Callable[[Run], None] | None = field(default=None, repr=False)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stages: dict[str, _Stage] = field(default_factory=dict, repr=False)
    _events: deque[Progress] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS), repr=False)
    _by_model: dict[str, _Usage] = field(default_factory=dict, repr=False)
    _by_role: dict[str, _Usage] = field(default_factory=dict, repr=False)
    _by_stage: dict[str, _Usage] = field(default_factory=dict, repr=False)
    _embedding_tokens: int = field(default=0, repr=False)
    _searches: int = field(default=0, repr=False)
    _watchers: list[_Watcher] = field(default_factory=list, repr=False)
    _calls: dict[int, LmCall] = field(default_factory=dict, repr=False)
    _contents: dict[int, LmCallContent] = field(default_factory=dict, repr=False)
    _next_call: int = field(default=0, repr=False)
    _next_event: int = field(default=0, repr=False)

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

    def set_result(self, result: RunResult) -> None:
        with self._lock:
            self.result = result

    def _finish(self, state: str, error: str | None) -> None:
        with self._lock:
            self.state = state
            self.error = error
            self.ended_at = time.time()
            if self.stage is not None:
                self._stages[self.stage].leave(self.ended_at)
        if self.on_end is not None:
            self.on_end(self)
        self._notify(_END)

    def record_progress(self, event: Progress) -> None:
        with self._lock:
            event.seq = self._next_event
            self._next_event += 1
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

    def start_lm(
        self, model: str, role: str | None, messages: list[dict[str, Any]] | None = None
    ) -> int:
        with self._lock:
            call = self._next_call
            self._next_call += 1
            if self.keep_content and messages is not None:
                self._contents[call] = LmCallContent(messages=messages, response=None)
            self._calls[call] = LmCall(
                id=call,
                event=_event.get(),
                has_content=call in self._contents,
                role=role,
                model=model,
                started_at=time.time(),
                ended_at=None,
                prompt_tokens=None,
                completion_tokens=None,
                failed=False,
            )
            # Insertion-ordered, so this forgets the oldest. A call forgotten while still in
            # flight is ended into nothing.
            while len(self._calls) > MAX_CALLS:
                forgotten = next(iter(self._calls))
                del self._calls[forgotten]
                self._contents.pop(forgotten, None)
            return call

    def end_lm(
        self,
        call: int,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        response: str | None = None,
        reasoning: str | None = None,
    ) -> None:
        with self._lock:
            record = self._calls.get(call)
            if record is None:
                return
            record.ended_at = time.time()
            record.prompt_tokens = prompt_tokens
            record.completion_tokens = completion_tokens
            record.failed = prompt_tokens is None
            content = self._contents.get(call)
            if content is not None:
                content.response = response
                content.reasoning = reasoning
            if not record.failed:
                seconds = record.ended_at - record.started_at
                for usage in (
                    self._by_model.setdefault(record.model, _Usage()),
                    self._by_role.setdefault(record.role or "unattributed", _Usage()),
                    self._stage_usage(),
                ):
                    usage.seconds += seconds

    def content(self, call: int) -> LmCallContent | None:
        with self._lock:
            content = self._contents.get(call)
            return None if content is None else content.model_copy(deep=True)

    def contents(self) -> dict[int, LmCallContent]:
        with self._lock:
            return {call: content.model_copy(deep=True) for call, content in self._contents.items()}

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
                has_result=self.result is not None,
            )

    def status(self, calls: int | None = STATUS_CALLS) -> RunStatus:
        """The run as it stands, with its most recent `calls`, or every call when None."""
        summary = self.summary()
        with self._lock:
            now = self.ended_at or time.time()
            kept = list(self._calls.values())
            return RunStatus(
                **dict(summary),
                stages=[stage.model(name, now) for name, stage in self._stages.items()],
                calls=[call.model_copy() for call in (kept if calls is None else kept[-calls:])],
            )

    def detail(self) -> RunDetail:
        status = self.status(calls=None)
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
    """The most recent runs, newest first. Older ones fall off the end.

    Memory holds the runs this process started. A store, when there is one, holds those of earlier
    processes too, and answers for any run memory has dropped.
    """

    def __init__(
        self, limit: int, store: RunStore | None = None, keep_content: bool = False
    ) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        self._limit = limit
        self._store = store
        self._keep_content = keep_content

    def start(self, kind: str, topic: str, planned_stages: list[str] | None = None) -> Run:
        run = Run(
            id=uuid4().hex[:12],
            kind=kind,
            topic=topic,
            planned_stages=planned_stages or [],
            keep_content=self._keep_content,
        )
        if self._store is not None:
            run.on_end = self._store.save
            self._store.save(run)
        with self._lock:
            self._runs[run.id] = run
            while len(self._runs) > self._limit:
                # Insertion-ordered, so this drops the oldest.
                del self._runs[next(iter(self._runs))]
        return run

    def get(self, run_id: str) -> Run | None:
        """A run this process holds in memory, which is the only kind that can be followed live."""
        with self._lock:
            return self._runs.get(run_id)

    def summaries(self) -> list[RunSummary]:
        with self._lock:
            live = list(reversed(self._runs.values()))
        summaries = [run.summary() for run in live]
        if self._store is not None:
            held = {summary.id for summary in summaries}
            summaries += [stored for stored in self._store.summaries() if stored.id not in held]
            summaries.sort(key=lambda summary: summary.created_at, reverse=True)
        return summaries[: self._limit]

    def detail(self, run_id: str) -> RunDetail:
        run = self.get(run_id)
        if run is not None:
            return run.detail()
        stored = self._store.detail(run_id) if self._store is not None else None
        if stored is None:
            raise RunNotFound(run_id)
        return stored

    def content(self, run_id: str, call: int) -> LmCallContent:
        run = self.get(run_id)
        if run is not None:
            content = run.content(call)
        elif self._store is not None:
            content = self._store.content(run_id, call)
        else:
            content = None
        if content is None:
            raise CallContentNotFound(f"{call} of run {run_id}")
        return content


@contextmanager
def _current_run(run: Run) -> Iterator[None]:
    token = _current.set(run)
    event = _event.set(None)
    try:
        yield
    finally:
        _event.reset(event)
        _current.reset(token)


def record_progress(event: Progress) -> None:
    """Attribute a progress event to whichever run this thread is working on, if any.

    Calls made afterwards in this thread, and in work it hands to a pool, are linked to it.
    """
    run = _current.get()
    if run is not None:
        run.record_progress(event)
        _event.set(event.seq)
