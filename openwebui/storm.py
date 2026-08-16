"""
title: STORM
author: brewcoua
author_url: https://github.com/brewcoua/kvasir
version: 0.1.0
license: MIT
description: Research a topic with Stanford STORM and return a cited article.
"""

# Paste this into Open WebUI under Admin, Functions. It is not reconciled from git.
#
# Conversation history is deliberately ignored. STORM researches a topic from scratch, so only the
# last user message is used, and a follow-up message starts a new run rather than continuing one.
#
# Uses only aiohttp and the standard library, both of which Open WebUI already ships. There is no
# requirements: frontmatter on purpose.

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

HEARTBEAT_SECONDS = 15


class Pipe:
    class Valves(BaseModel):
        KVASIR_URL: str = Field(
            default="http://kvasir:8080",
            description="Base URL of the kvasir service.",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=3600,
            description=(
                "Total timeout for one run. A default run takes minutes to tens of minutes, so "
                "this is deliberately far longer than a normal HTTP client default."
            ),
        )
        MODEL_FAST: str = Field(
            default="",
            description=(
                "Overrides the service's fast model, used for conversation simulation, question "
                "asking and polishing. Leave empty to use the service default."
            ),
        )
        MODEL_STRONG: str = Field(
            default="",
            description=(
                "Overrides the service's strong model, used for outline and article generation. "
                "Leave empty to use the service default."
            ),
        )
        SEARCH_TOP_K: int = Field(
            default=0, description="Search results per query. 0 uses the service default."
        )
        MAX_CONV_TURN: int = Field(
            default=0, description="Questions per perspective. 0 uses the service default."
        )
        MAX_PERSPECTIVE: int = Field(
            default=0, description="Perspectives to research. 0 uses the service default."
        )
        POLISH: bool = Field(
            default=True, description="Run the polishing stage. Slower, better prose."
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def pipes(self) -> list[dict[str, str]]:
        return [{"id": "storm", "name": "STORM"}]

    async def pipe(
        self,
        body: dict[str, Any],
        __event_emitter__: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> str:
        topic = _last_user_message(body)
        if not topic:
            return "Send a topic to research."

        status = _Status(__event_emitter__)
        await status.say(f"Researching {topic}")

        heartbeat = asyncio.create_task(status.beat())
        try:
            return await self._run(topic, status)
        except TimeoutError:
            return (
                f"The run exceeded {self.valves.REQUEST_TIMEOUT_SECONDS} seconds and was abandoned."
            )
        except aiohttp.ClientError as error:
            return f"Could not reach kvasir at {self.valves.KVASIR_URL}: {error}"
        finally:
            heartbeat.cancel()
            await status.done()

    async def _run(self, topic: str, status: "_Status") -> str:
        timeout = aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT_SECONDS)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(
                f"{self.valves.KVASIR_URL.rstrip('/')}/v1/research",
                json=self._request(topic),
            ) as response,
        ):
            if response.status != 200:
                return f"kvasir returned {response.status}: {(await response.text())[:500]}"

            async for event, data in _events(response):
                if event == "progress":
                    await status.say(f"{data['stage']}: {data['detail']}")
                elif event == "error":
                    return f"The run failed: {data['message']}"
                elif event == "done":
                    return _article(data)

        return "The run ended without producing an article."

    def _request(self, topic: str) -> dict[str, Any]:
        # Empty and zero mean "leave it to the service", so its defaults stay in one place.
        optional = {
            "model_fast": self.valves.MODEL_FAST,
            "model_strong": self.valves.MODEL_STRONG,
            "search_top_k": self.valves.SEARCH_TOP_K,
            "max_conv_turn": self.valves.MAX_CONV_TURN,
            "max_perspective": self.valves.MAX_PERSPECTIVE,
        }
        request = {"topic": topic, "do_polish_article": self.valves.POLISH}
        request.update({name: value for name, value in optional.items() if value})
        return request


class _Status:
    """Status events, plus a heartbeat so a long silent stage does not look like a hang."""

    def __init__(self, emitter: Callable[[dict[str, Any]], Awaitable[None]] | None) -> None:
        self._emitter = emitter
        self._started = time.monotonic()
        self._latest = "Starting"

    async def say(self, description: str) -> None:
        self._latest = description
        await self._emit(description)

    async def beat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self._emit(f"{self._latest} ({self._elapsed()})")

    async def done(self) -> None:
        await self._emit(f"Finished in {self._elapsed()}", done=True)

    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - self._started)
        return f"{seconds // 60}m {seconds % 60}s"

    async def _emit(self, description: str, done: bool = False) -> None:
        if self._emitter:
            await self._emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )


async def _events(response: aiohttp.ClientResponse) -> Any:
    """Yield (event, payload) pairs from an SSE body. A blank line terminates a frame."""
    event = ""
    async for raw in response.content:
        line = raw.decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            yield event, json.loads(line.removeprefix("data: "))


def _article(result: dict[str, Any]) -> str:
    citations = result.get("citations") or []
    references = "\n".join(
        f"{citation['index']}. [{citation['title'] or citation['url']}]({citation['url']})"
        for citation in citations
    )
    duration = int(result.get("duration_seconds", 0))
    footer = f"\n\n---\n\n_{len(citations)} sources, {duration // 60}m {duration % 60}s_"
    article = result.get("article", "")
    return f"{article}\n\n## References\n\n{references}{footer}" if references else article + footer


def _last_user_message(body: dict[str, Any]) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            # Multimodal messages carry a list of parts; only the text matters for a topic.
            if isinstance(content, list):
                return " ".join(
                    part.get("text", "") for part in content if part.get("type") == "text"
                ).strip()
    return ""
