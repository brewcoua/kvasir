"""The most recent runs, in memory and, with KVASIR_RUN_STORE=sqlite, in the store."""

from __future__ import annotations

import threading
from uuid import uuid4

from kvasir.runs.run import Run
from kvasir.runs.store import RunStore
from kvasir.schemas.runs import LmCallContent, RunDetail, RunSummary


class RunNotFound(Exception):
    """No run with that id. Either it never existed or it aged out of retention."""


class CallContentNotFound(Exception):
    """No content for that call: keeping it is off, the call is unknown, or it aged out."""


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
