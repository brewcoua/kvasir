"""
title: Co-STORM
author: brewcoua
author_url: https://github.com/brewcoua/kvasir
version: 0.1.0
license: MIT
description: A round-table research conversation with Stanford Co-STORM.
"""

# Paste this into Open WebUI under Admin, Functions. It is not reconciled from git.
#
# The session is keyed by the Open WebUI chat id, so one chat is one round table and it survives a
# restart of the service. The first message is the topic and runs warm start, which is slow.
# Afterwards:
#
#   next     advance the round table without speaking
#   report   generate the report so far
#   anything else is said to the round table
#
# A leading slash is stripped, so "/next" works too. Open WebUI refuses to send a message whose
# leading /token is not registered, and registering one is a Prompt under Workspace, not part of
# this function, so the bare words are the path that needs no extra setup.
#
# This duplicates a little of storm.py on purpose. Open WebUI functions cannot import each other.

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

HEARTBEAT_SECONDS = 15
ADVANCE = "next"
REPORT = "report"


class Pipe:
    class Valves(BaseModel):
        KVASIR_URL: str = Field(
            default="http://kvasir:8080",
            description="Base URL of the kvasir service.",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=1800,
            description=(
                "Total timeout for one call. Warm start is the slow one, taking minutes, so this "
                "is deliberately far longer than a normal HTTP client default."
            ),
        )
        MODEL_FAST: str = Field(
            default="",
            description=(
                "Overrides the service's fast model, used for discourse management, question "
                "asking and polishing. Leave empty to use the service default."
            ),
        )
        MODEL_STRONG: str = Field(
            default="",
            description=(
                "Overrides the service's strong model, used for answering and the mind map. "
                "Leave empty to use the service default."
            ),
        )
        ADVANCE_AFTER_WARM_START: bool = Field(
            default=True,
            description=(
                "Take one agent turn immediately after warm start, so the first reply has "
                "something to read rather than only a list of experts."
            ),
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def pipes(self) -> list[dict[str, str]]:
        return [{"id": "costorm", "name": "Co-STORM"}]

    async def pipe(
        self,
        body: dict[str, Any],
        __event_emitter__: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        __metadata__: dict[str, Any] | None = None,
    ) -> str:
        message = _last_user_message(body)
        if not message:
            return "Send a topic to start a round table."

        session_id = (__metadata__ or {}).get("chat_id") or body.get("chat_id")
        if not session_id:
            return "Open WebUI did not supply a chat id, so this conversation cannot be tracked."

        status = _Status(__event_emitter__)
        heartbeat = asyncio.create_task(status.beat())
        try:
            return await self._act(str(session_id), message, status)
        except _ServiceError as error:
            return str(error)
        except TimeoutError:
            limit = self.valves.REQUEST_TIMEOUT_SECONDS
            return f"The call exceeded {limit} seconds and was abandoned."
        except aiohttp.ClientError as error:
            return f"Could not reach kvasir at {self.valves.KVASIR_URL}: {error}"
        finally:
            heartbeat.cancel()
            await status.done()

    async def _act(self, session_id: str, message: str, status: "_Status") -> str:
        timeout = aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            if not await self._session_exists(http, session_id):
                await status.say(f"Warm starting on {message}")
                return await self._start(http, session_id, message, status)

            command = message.strip().lstrip("/").lower()
            if command == REPORT:
                await status.say("Generating the report")
                return await self._report(http, session_id)

            if command == ADVANCE:
                await status.say("Advancing the round table")
                return _turn(await self._step(http, session_id, "", status))

            await status.say("Taking your turn")
            said = await self._step(http, session_id, message, status)
            # A user turn is recorded rather than answered, so the answer needs a second step.
            await status.say("Waiting for a response")
            return _turn(await self._step(http, session_id, "", status), spoken=said)

    async def _start(
        self, http: aiohttp.ClientSession, session_id: str, topic: str, status: "_Status"
    ) -> str:
        request: dict[str, Any] = {"session_id": session_id, "topic": topic}
        if self.valves.MODEL_FAST:
            request["model_fast"] = self.valves.MODEL_FAST
        if self.valves.MODEL_STRONG:
            request["model_strong"] = self.valves.MODEL_STRONG

        info = await self._stream(http, "POST", "/v1/session", status, request)
        experts = ", ".join(info.get("experts") or []) or "none yet"
        opening = f"Round table on **{info.get('topic', topic)}**.\n\nExperts: {experts}."

        if self.valves.ADVANCE_AFTER_WARM_START:
            await status.say("Taking the first turn")
            turn = await self._step(http, session_id, "", status)
            return f"{opening}\n\n---\n\n{_turn(turn)}"

        return f"{opening}\n\nSay `{ADVANCE}` to advance, `{REPORT}` for a report, or just talk."

    async def _step(
        self, http: aiohttp.ClientSession, session_id: str, utterance: str, status: "_Status"
    ) -> dict[str, Any]:
        return await self._stream(
            http, "POST", f"/v1/session/{session_id}/step", status, {"utterance": utterance}
        )

    async def _report(self, http: aiohttp.ClientSession, session_id: str) -> str:
        async with http.post(self._url(f"/v1/session/{session_id}/report")) as response:
            if response.status != 200:
                raise _ServiceError(response.status, await response.text())
            payload = await response.json()
        return payload["report"] + _references(payload.get("citations"))

    async def _session_exists(self, http: aiohttp.ClientSession, session_id: str) -> bool:
        async with http.get(self._url(f"/v1/session/{session_id}")) as response:
            return response.status == 200

    async def _stream(
        self,
        http: aiohttp.ClientSession,
        method: str,
        path: str,
        status: "_Status",
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Run one streaming call, reporting progress, and return its done payload."""
        async with http.request(method, self._url(path), json=payload) as response:
            if response.status != 200:
                raise _ServiceError(response.status, await response.text())

            async for event, data in _events(response):
                if event == "progress":
                    await status.say(f"{data['stage']}: {data['detail']}")
                elif event == "error":
                    raise _ServiceError(200, data["message"])
                elif event == "done":
                    return data
        raise _ServiceError(200, "the call ended without a result")

    def _url(self, path: str) -> str:
        return f"{self.valves.KVASIR_URL.rstrip('/')}{path}"


class _ServiceError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"kvasir returned {status}: {detail[:500]}" if status != 200 else detail)


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
    """Yield (event, payload) pairs from an SSE body."""
    event = ""
    async for raw in response.content:
        line = raw.decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            yield event, json.loads(line.removeprefix("data: "))


def _turn(turn: dict[str, Any], spoken: dict[str, Any] | None = None) -> str:
    role = turn.get("role") or "Round table"
    description = turn.get("role_description") or ""
    header = f"**{role}**" + (f" _{description.strip()}_" if description.strip() else "")

    parts = [header, "", turn.get("utterance", "")]
    if turn.get("mind_map_reorganised"):
        parts.append("\n_The mind map was reorganised._")
    parts.append(_references(turn.get("citations")))

    # The user's own recorded turn carries no content worth repeating, only confirmation.
    if spoken is not None and spoken.get("utterance_type"):
        parts.append(f"\n_Recorded as {spoken['utterance_type']}._")
    return "\n".join(parts).strip()


def _references(citations: list[dict[str, Any]] | None) -> str:
    if not citations:
        return ""
    entries = "\n".join(
        f"{citation['index']}. [{citation['title'] or citation['url']}]({citation['url']})"
        for citation in citations
    )
    return f"\n\n**Sources**\n\n{entries}"


def _last_user_message(body: dict[str, Any]) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                return " ".join(
                    part.get("text", "") for part in content if part.get("type") == "text"
                ).strip()
    return ""
