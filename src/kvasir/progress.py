"""Carrying progress out of a running pipeline and into an SSE response.

STORM runs synchronously in a worker thread while the response is served from the event loop, so
the two sides are connected by an asyncio queue fed through `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from kvasir import logs
from kvasir.models import Progress
from kvasir.storm.collaborative_storm.modules.callback import (
    BaseCallbackHandler as CoStormBaseCallbackHandler,
)
from kvasir.storm.storm_wiki.modules.callback import BaseCallbackHandler

RESEARCH = "research"
OUTLINE = "outline"
ARTICLE = "article"
POLISH = "polish"

WARM_START = "warm_start"
TURN = "turn"
MIND_MAP = "mind_map"

logger = logging.getLogger(__name__)


class ProgressStream:
    """A queue of `Progress` events, written from a worker thread and read on the event loop."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Progress | None] = asyncio.Queue()

    def publish(self, stage: str, detail: str) -> None:
        """Queue an event, and log it. Safe to call from any thread.

        Setting the stage here rather than at each call site keeps the two in step: a progress
        event is exactly the moment the stage is known to have changed. Threads the pipeline
        spawns afterwards inherit it.
        """
        logs.set_stage(stage)
        logger.info("%s", detail)
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
    """Publishes STORM's callbacks onto a `ProgressStream`.

    Called from STORM's own worker threads, so it holds nothing but two counters, both guarded
    because sections and conversation turns are both produced concurrently.
    """

    def __init__(self, stream: ProgressStream) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._turns = 0
        self._sections = 0
        self._sections_done = 0

    def on_identify_perspective_start(self, **kwargs: Any) -> None:
        self._stream.publish(RESEARCH, "identifying perspectives")

    def on_identify_perspective_end(self, perspectives: list[str], **kwargs: Any) -> None:
        self._stream.publish(RESEARCH, f"identified {len(perspectives)} perspectives")

    def on_information_gathering_start(self, **kwargs: Any) -> None:
        self._stream.publish(RESEARCH, "searching and asking questions")

    def on_dialogue_turn_end(self, dlg_turn: Any, **kwargs: Any) -> None:
        with self._lock:
            self._turns += 1
            turns = self._turns
        self._stream.publish(RESEARCH, f"completed conversation turn {turns}")

    def on_information_gathering_end(self, **kwargs: Any) -> None:
        self._stream.publish(RESEARCH, f"gathered information over {self._turns} turns")

    def on_information_organization_start(self, **kwargs: Any) -> None:
        self._stream.publish(OUTLINE, "organising what was found")

    def on_direct_outline_generation_end(self, outline: str, **kwargs: Any) -> None:
        self._stream.publish(OUTLINE, "drafted a first outline")

    def on_outline_refinement_end(self, outline: str, **kwargs: Any) -> None:
        self._stream.publish(OUTLINE, "refined the outline")

    def on_article_generation_start(self, sections: list[str], **kwargs: Any) -> None:
        self._sections = len(sections)
        self._stream.publish(ARTICLE, f"writing {self._sections} sections with citations")

    def on_section_generation_start(self, section: str, **kwargs: Any) -> None:
        self._stream.publish(ARTICLE, f"writing {section}")

    def on_section_generation_end(self, section: str, **kwargs: Any) -> None:
        # Sections are written concurrently, so this counts completions rather than tracking which
        # section is current.
        with self._lock:
            self._sections_done += 1
            done = self._sections_done
        self._stream.publish(ARTICLE, f"finished {section} ({done}/{self._sections})")

    def on_article_generation_end(self, **kwargs: Any) -> None:
        self._stream.publish(ARTICLE, "assembled the article")

    def on_polish_start(self, **kwargs: Any) -> None:
        self._stream.publish(POLISH, "polishing the article")

    def on_polish_end(self, **kwargs: Any) -> None:
        self._stream.publish(POLISH, "polished the article")


class CoStormProgressHandler(CoStormBaseCallbackHandler):
    """Publishes Co-STORM's callbacks, and records whether the mind map was reorganised.

    This is a different class from the STORM handler above. Upstream ships two `BaseCallbackHandler`
    types that share a name and nothing else, one per engine.
    """

    def __init__(self, stream: ProgressStream) -> None:
        self._stream = stream
        self.mind_map_reorganised = False

    def on_warmstart_update(self, message: str, **kwargs: Any) -> None:
        self._stream.publish(WARM_START, message)

    def on_turn_policy_planning_start(self, **kwargs: Any) -> None:
        self._stream.publish(TURN, "deciding who speaks next")

    def on_expert_action_planning_start(self, **kwargs: Any) -> None:
        self._stream.publish(TURN, "planning the next contribution")

    def on_expert_information_collection_start(self, **kwargs: Any) -> None:
        self._stream.publish(TURN, "searching for sources")

    def on_expert_information_collection_end(self, info: list[Any], **kwargs: Any) -> None:
        self._stream.publish(TURN, f"collected {len(info)} sources")

    def on_expert_utterance_generation_end(self, **kwargs: Any) -> None:
        self._stream.publish(TURN, "drafted a response")

    def on_expert_utterance_polishing_start(self, **kwargs: Any) -> None:
        self._stream.publish(TURN, "polishing the response")

    def on_mindmap_insert_start(self, **kwargs: Any) -> None:
        self._stream.publish(MIND_MAP, "filing what was learned")

    def on_mindmap_reorg_start(self, **kwargs: Any) -> None:
        # The only signal that the mind map changed shape, which a turn response reports.
        self.mind_map_reorganised = True
        self._stream.publish(MIND_MAP, "reorganising the mind map")

    def on_expert_list_update_start(self, **kwargs: Any) -> None:
        self._stream.publish(TURN, "updating the expert list")

    def on_article_generation_start(self, **kwargs: Any) -> None:
        self._stream.publish(ARTICLE, "writing the report")
