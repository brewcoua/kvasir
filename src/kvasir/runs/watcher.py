"""Following a run live from the event loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from kvasir.schemas.runs import Progress

# Ends a watcher's stream. Distinct from None, which a watcher with a heartbeat yields when idle.
END: Any = object()


class Watcher:
    """One live follower of a run.

    Events arrive on pipeline threads and are read from the event loop, so each watcher remembers
    the loop it was created on and hands the event over through it.
    """

    def __init__(self, heartbeat: float | None) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._heartbeat = heartbeat

    def push(self, event: Any) -> None:
        # `END` means the run reached a terminal state, which ends the stream.
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def __aiter__(self) -> AsyncIterator[Progress | None]:
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), self._heartbeat)
            except TimeoutError:
                yield None
                continue
            if event is END:
                return
            yield event
