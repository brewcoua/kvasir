"""Carrying progress out of a running pipeline and into an SSE response.

STORM runs synchronously in a worker thread while the response is served from the event loop, so
the two sides are connected by an asyncio queue fed through `call_soon_threadsafe`.

Upstream's callbacks cover the research and outline stages only. There is no callback for article
generation or polishing, so those stages are published by whatever drives the run. That is why
publishing is a method on the stream rather than something only the handler can do.
"""

from __future__ import annotations

import asyncio
from typing import Any

from knowledge_storm.storm_wiki.modules.callback import BaseCallbackHandler

from kvasir.models import Progress

RESEARCH = "research"
OUTLINE = "outline"
ARTICLE = "article"
POLISH = "polish"


class ProgressStream:
    """A queue of `Progress` events, written from a worker thread and read on the event loop."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Progress | None] = asyncio.Queue()

    def publish(self, stage: str, detail: str) -> None:
        """Queue an event. Safe to call from any thread."""
        event = Progress(stage=stage, detail=detail)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def close(self) -> None:
        """Signal that no further events will arrive, ending iteration."""
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    async def __aiter__(self) -> Any:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


class StormProgressHandler(BaseCallbackHandler):
    """Publishes upstream's research and outline callbacks onto a `ProgressStream`.

    Called from STORM's own worker threads, so it holds no state beyond a turn counter, and the
    counter only ever grows.
    """

    def __init__(self, stream: ProgressStream) -> None:
        self._stream = stream
        self._turns = 0

    def on_identify_perspective_start(self, **kwargs: Any) -> None:
        self._stream.publish(RESEARCH, "identifying perspectives")

    def on_identify_perspective_end(self, perspectives: list[str], **kwargs: Any) -> None:
        self._stream.publish(RESEARCH, f"identified {len(perspectives)} perspectives")

    def on_information_gathering_start(self, **kwargs: Any) -> None:
        self._stream.publish(RESEARCH, "searching and asking questions")

    def on_dialogue_turn_end(self, dlg_turn: Any, **kwargs: Any) -> None:
        self._turns += 1
        self._stream.publish(RESEARCH, f"completed conversation turn {self._turns}")

    def on_information_gathering_end(self, **kwargs: Any) -> None:
        self._stream.publish(RESEARCH, f"gathered information over {self._turns} turns")

    def on_information_organization_start(self, **kwargs: Any) -> None:
        self._stream.publish(OUTLINE, "organising what was found")

    def on_direct_outline_generation_end(self, outline: str, **kwargs: Any) -> None:
        self._stream.publish(OUTLINE, "drafted a first outline")

    def on_outline_refinement_end(self, outline: str, **kwargs: Any) -> None:
        self._stream.publish(OUTLINE, "refined the outline")
