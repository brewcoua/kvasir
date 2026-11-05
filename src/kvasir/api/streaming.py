"""Running blocking work in a thread, and streaming its progress as server-sent events."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable

from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import State

from kvasir.api.sse import HEADERS, MEDIA_TYPE, frame
from kvasir.research.sources import Sources, with_sources
from kvasir.runs.progress import ProgressStream
from kvasir.runs.run import Run
from kvasir.schemas.results import RunResult
from kvasir.schemas.runs import Error, RunStarted

logger = logging.getLogger("kvasir")


class RunSlots:
    """How many runs may go at once. Unlike a semaphore its `limit` can change while runs hold
    slots: a lower one refuses new runs until enough have finished."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._taken = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        """Takes a slot if one is free. Never waits."""
        with self._lock:
            if self._taken >= self.limit:
                return False
            self._taken += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._taken -= 1


def streamed(
    state: State,
    work: Callable[[ProgressStream], RunResult],
    description: str,
    kind: str,
    topic: str,
    planned_stages: list[str] | None = None,
) -> Response:
    """Run blocking work in a thread, streaming progress until it produces a result.

    Returns 429 rather than queueing when every run slot is taken, because a queued request would
    wait longer than any sensible client timeout, and a refusal consumes no slot.
    """
    state.settings.ready()
    # Registered before the slot is claimed, so a refusal is a run that shows up as rejected rather
    # than a 429 with nothing behind it. Saturation is the case where the registry earns its keep.
    run = state.runs.start(kind, topic, planned_stages)
    if not state.run_slots.acquire():
        run.reject()
        logger.warning("rejected %s: every run slot is busy", description)
        return JSONResponse({"message": "all run slots are busy, retry later"}, status_code=429)

    return StreamingResponse(
        events(work, description, run, state.run_slots, state.sources),
        media_type=MEDIA_TYPE,
        headers=HEADERS,
    )


async def events(
    work: Callable[[ProgressStream], RunResult],
    description: str,
    record: Run,
    run_slots: RunSlots,
    sources: Sources | None = None,
) -> AsyncIterator[str]:
    stream = ProgressStream()

    def run() -> RunResult:
        started = time.monotonic()
        try:
            # Entered on the worker thread, so the run identity is scoped to this run rather than
            # shared with the event loop, and the pipeline's pools inherit it with the context.
            with record.active():
                logger.info("started %s", description)
                result = work(stream)
                # Before the run ends, so the closing snapshot already carries it.
                record.set_result(result)
                return result
        finally:
            logger.info("finished %s in %.1fs", description, time.monotonic() - started)
            # Here rather than when the response ends, which is earlier if the client leaves.
            run_slots.release()
            # Ends the iteration below even when the work raises.
            stream.close()

    worker: asyncio.Task[RunResult] | None = None
    try:
        # First, so a client can follow the run at /v1/runs/{id} even if it stops reading here.
        yield frame("run", RunStarted(run_id=record.id))
        # A disconnecting client does not stop the work. Upstream offers no way to cancel a run,
        # and the thread carries on to completion.
        worker = asyncio.create_task(asyncio.to_thread(run))
        async for event in stream:
            yield frame("progress", event)

        result = await worker
        yield frame("done", await asyncio.to_thread(with_sources, sources, result))
    except Exception as exc:
        # Raised on the event loop rather than in the run's thread, so the run is named explicitly.
        logger.exception(
            "%s failed", description, extra={"run_id": record.id, "run_kind": record.kind}
        )
        yield frame("error", Error(message=f"{type(exc).__name__}: {exc}"))
    finally:
        # Once started, the work releases the slot itself.
        if worker is None:
            run_slots.release()
