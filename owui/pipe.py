"""
title: kvasir
author: brewcoua
author_url: https://github.com/brewcoua/kvasir
version: 0.2.0
license: MIT
description: Stanford STORM and Co-STORM, as two models backed by a kvasir service.
"""

# Import this into Open WebUI under Admin, Functions, Import From Link, with the id `kvasir`. It
# is read once and not reconciled from git, so repeat it after changing this file.
#
# One function, two models. Open WebUI namespaces a pipe as <function id>.<pipe id>, so the ids
# below become `kvasir.storm` and `kvasir.co-storm`.
#
# STORM ignores conversation history: it researches a topic from scratch, so only the last user
# message is used and a follow-up starts a new run. Co-STORM keys its session by the chat id, so
# one chat is one round table, and understands `next` and `report`.
#
# A run takes minutes to tens of minutes. Progress is written live into a reasoning block, which
# Open WebUI renders collapsed and times itself, so the message says what is happening rather than
# sitting empty.
#
# Uses only aiohttp, pydantic and the standard library, all of which Open WebUI already ships.
# There is no requirements: frontmatter on purpose.

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal

import aiohttp
from pydantic import BaseModel, Field

HEARTBEAT_SECONDS = 15
STORM = "storm"
CO_STORM = "co-storm"
ADVANCE = "next"
REPORT = "report"

Emit = Callable[[dict[str, Any]], Awaitable[Any]] | None


class Pipe:
    class Valves(BaseModel):
        KVASIR_URL: str = Field(
            default="http://kvasir:8080",
            description="Base URL of the kvasir service.",
        )
        RESEARCH_TIMEOUT_SECONDS: int = Field(
            default=3600,
            description=(
                "Total timeout for one STORM run. A default run takes minutes to tens of minutes, "
                "so this is deliberately far longer than a normal HTTP client default."
            ),
        )
        SESSION_TIMEOUT_SECONDS: int = Field(
            default=1800,
            description=(
                "Total timeout for one Co-STORM call. Warm start is the slow one, taking minutes."
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
        ADVANCE_AFTER_WARM_START: bool = Field(
            default=True,
            description=(
                "Co-STORM: take one agent turn immediately after warm start, so the first reply "
                "has something to read rather than only a list of experts."
            ),
        )
        CONFIRM_RERUN: bool = Field(
            default=True,
            description=(
                "STORM: ask before researching again in a chat that already holds an article. "
                "A follow-up message is a whole new run, not a continuation."
            ),
        )
        SHOW_USAGE: bool = Field(
            default=True,
            description="Append what the run spent: stage timings, tokens and cost.",
        )
        SET_CHAT_TITLE: bool = Field(
            default=True,
            description="Name the chat after the topic, on the first message only.",
        )

    class UserValves(BaseModel):
        """Per-user overrides. Zero and `default` defer to the admin valve above."""

        SEARCH_TOP_K: int = Field(default=0, description="Search results per query.")
        MAX_CONV_TURN: int = Field(default=0, description="Questions per perspective.")
        MAX_PERSPECTIVE: int = Field(default=0, description="Perspectives to research.")
        POLISH: Literal["default", "on", "off"] = Field(
            default="default", description="Run the polishing stage."
        )
        SHOW_USAGE: Literal["default", "on", "off"] = Field(
            default="default", description="Append what the run spent."
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def pipes(self) -> list[dict[str, str]]:
        return [{"id": STORM, "name": "STORM"}, {"id": CO_STORM, "name": "Co-STORM"}]

    async def pipe(
        self,
        body: dict[str, Any],
        __event_emitter__: Emit = None,
        __event_call__: Emit = None,
        __metadata__: dict[str, Any] | None = None,
        __user__: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        kind = str(body.get("model") or "").rsplit(".", 1)[-1]
        options = _Options(self.valves, __user__)
        chat = _Chat(__event_emitter__)
        status = _Status(__event_emitter__)
        think = _Reasoning()

        message = _last_user_message(body)
        if not message:
            prompt = "a topic to research" if kind == STORM else "a topic for the round table"
            message = await _ask(__event_call__, f"Send {prompt}.")
        if not message:
            yield f"Send {'a topic to research' if kind == STORM else 'a topic'}."
            return

        heartbeat = asyncio.create_task(status.beat())
        try:
            if kind == CO_STORM:
                work = self._costorm(body, message, options, chat, status, think, __metadata__)
            else:
                work = self._storm(body, message, options, chat, status, think, __event_call__)
            async for chunk in work:
                if chunk:
                    yield chunk
            # Nothing ran if the block never opened, and "finished" would be a lie.
            if think.used:
                await chat.notify("success", f"Finished in {status.elapsed()}")
        except _ServiceError as error:
            yield think.close()
            yield str(error)
            await chat.notify("error", str(error))
        except TimeoutError:
            note = f"The call exceeded its timeout after {status.elapsed()} and was abandoned."
            yield think.close()
            yield note
            await chat.notify("error", note)
        except aiohttp.ClientError as error:
            note = f"Could not reach kvasir at {self.valves.KVASIR_URL}: {error}"
            yield think.close()
            yield note
            await chat.notify("error", note)
        finally:
            heartbeat.cancel()
            await status.done()

    # -- STORM ---------------------------------------------------------------------------------

    async def _storm(
        self,
        body: dict[str, Any],
        topic: str,
        options: "_Options",
        chat: "_Chat",
        status: "_Status",
        think: "_Reasoning",
        call: Emit,
    ) -> AsyncIterator[str]:
        # History is not continued, so a follow-up silently burns another full run. Say so first.
        if self.valves.CONFIRM_RERUN and _has_answer(body):
            confirmed = await _confirm(
                call,
                "Research this from scratch?",
                "STORM does not continue a conversation. This starts a new run, which takes "
                "minutes to tens of minutes.",
            )
            if not confirmed:
                yield "Cancelled. Start a new chat, or confirm to research again."
                return

        await chat.begin(body, STORM, topic, self.valves.SET_CHAT_TITLE)
        await status.say(f"Researching {topic}")

        out: dict[str, Any] = {}
        timeout = aiohttp.ClientTimeout(total=self.valves.RESEARCH_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            yield think.start()
            request = self._request(topic, options)
            async for line in self._call(http, "POST", "/v1/research", request, think, status, out):
                yield line
            yield think.close()

            result = out.get("result")
            if result is None:
                yield "The run ended without producing an article."
                return

            yield result.get("article", "")
            await chat.sources(result.get("citations"))
            yield await self._spent(http, out.get("run_id"), options)

    def _request(self, topic: str, options: "_Options") -> dict[str, Any]:
        # Empty and zero mean "leave it to the service", so its defaults stay in one place.
        optional = {
            "model_fast": self.valves.MODEL_FAST,
            "model_strong": self.valves.MODEL_STRONG,
            "search_top_k": options.search_top_k,
            "max_conv_turn": options.max_conv_turn,
            "max_perspective": options.max_perspective,
        }
        request: dict[str, Any] = {"topic": topic, "do_polish_article": options.polish}
        request.update({name: value for name, value in optional.items() if value})
        return request

    # -- Co-STORM ------------------------------------------------------------------------------

    async def _costorm(
        self,
        body: dict[str, Any],
        message: str,
        options: "_Options",
        chat: "_Chat",
        status: "_Status",
        think: "_Reasoning",
        metadata: dict[str, Any] | None,
    ) -> AsyncIterator[str]:
        session_id = (metadata or {}).get("chat_id") or body.get("chat_id")
        if not session_id:
            yield "Open WebUI did not supply a chat id, so this conversation cannot be tracked."
            return
        session_id = str(session_id)

        timeout = aiohttp.ClientTimeout(total=self.valves.SESSION_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            if not await self._exists(http, session_id):
                if message.strip().lstrip("/").lower() in (ADVANCE, REPORT):
                    yield "No round table in this chat yet. Send a topic to start one."
                    return
                await chat.begin(body, CO_STORM, message, self.valves.SET_CHAT_TITLE)
                start = self._start(http, session_id, message, options, chat, status, think)
                async for chunk in start:
                    yield chunk
                return

            command = message.strip().lstrip("/").lower()
            out: dict[str, Any] = {}
            yield think.start()

            if command == REPORT:
                await status.say("Generating the report")
                path = f"/v1/session/{session_id}/report"
                async for line in self._call(http, "POST", path, {}, think, status, out):
                    yield line
                yield think.close()
                yield out["result"]["report"]
                await chat.sources(out["result"].get("citations"))
                yield await self._spent(http, out.get("run_id"), options)
                return

            if command == ADVANCE:
                await status.say("Advancing the round table")
            else:
                await status.say("Taking your turn")
                async for line in self._step(http, session_id, message, think, status, out):
                    yield line
                # A user turn is recorded rather than answered, so the answer needs a second step.
                yield think.line("turn", f"recorded as {out['result'].get('utterance_type')}")
                await status.say("Waiting for a response")

            async for line in self._step(http, session_id, "", think, status, out):
                yield line
            yield think.close()

            yield _turn(out["result"])
            await chat.sources(out["result"].get("citations"))
            yield await self._spent(http, out.get("run_id"), options)

    async def _start(
        self,
        http: aiohttp.ClientSession,
        session_id: str,
        topic: str,
        options: "_Options",
        chat: "_Chat",
        status: "_Status",
        think: "_Reasoning",
    ) -> AsyncIterator[str]:
        request: dict[str, Any] = {"session_id": session_id, "topic": topic}
        if self.valves.MODEL_FAST:
            request["model_fast"] = self.valves.MODEL_FAST
        if self.valves.MODEL_STRONG:
            request["model_strong"] = self.valves.MODEL_STRONG

        await status.say(f"Warm starting on {topic}")
        out: dict[str, Any] = {}
        yield think.start()
        async for line in self._call(http, "POST", "/v1/session", request, think, status, out):
            yield line

        info = out["result"]
        experts = ", ".join(info.get("experts") or []) or "none yet"

        if not self.valves.ADVANCE_AFTER_WARM_START:
            yield think.close()
            yield f"Round table on **{info.get('topic', topic)}**.\n\nExperts: {experts}.\n\n"
            yield f"Say `{ADVANCE}` to advance, `{REPORT}` for a report, or just talk."
            yield await self._spent(http, out.get("run_id"), options)
            return

        await status.say("Taking the first turn")
        turn: dict[str, Any] = {}
        async for line in self._step(http, session_id, "", think, status, turn):
            yield line
        yield think.close()

        yield f"Round table on **{info.get('topic', topic)}**.\n\nExperts: {experts}.\n\n---\n\n"
        yield _turn(turn["result"])
        await chat.sources(turn["result"].get("citations"))
        yield await self._spent(http, turn.get("run_id"), options)

    async def _step(
        self,
        http: aiohttp.ClientSession,
        session_id: str,
        utterance: str,
        think: "_Reasoning",
        status: "_Status",
        out: dict[str, Any],
    ) -> AsyncIterator[str]:
        async for line in self._call(
            http,
            "POST",
            f"/v1/session/{session_id}/step",
            {"utterance": utterance},
            think,
            status,
            out,
        ):
            yield line

    async def _exists(self, http: aiohttp.ClientSession, session_id: str) -> bool:
        async with http.get(self._url(f"/v1/session/{session_id}")) as response:
            return response.status == 200

    # -- Shared --------------------------------------------------------------------------------

    async def _call(
        self,
        http: aiohttp.ClientSession,
        method: str,
        path: str,
        payload: dict[str, Any],
        think: "_Reasoning",
        status: "_Status",
        out: dict[str, Any],
    ) -> AsyncIterator[str]:
        """Run one streaming call, yielding reasoning lines and leaving its result in `out`."""
        # Tracked separately from `out`, which carries the previous call's result on a second step.
        answered = False
        async with http.request(method, self._url(path), json=payload) as response:
            if response.status != 200:
                raise _ServiceError(response.status, await response.text())

            async for event, data in _events(response):
                if event == "run":
                    out["run_id"] = data["run_id"]
                elif event == "progress":
                    await status.say(f"{data['stage']}: {data['detail']}")
                    yield think.line(data["stage"], data["detail"])
                elif event == "error":
                    raise _ServiceError(200, data["message"])
                elif event == "done":
                    out["result"] = data
                    answered = True

        if not answered:
            raise _ServiceError(200, "the call ended without a result")

    async def _spent(
        self, http: aiohttp.ClientSession, run_id: str | None, options: "_Options"
    ) -> str:
        """What the run cost, from the registry. A footer, so a failure to read it is not one."""
        if not options.show_usage or not run_id:
            return ""
        try:
            async with http.get(self._url(f"/v1/runs/{run_id}")) as response:
                if response.status != 200:
                    return ""
                return _spent(await response.json())
        except aiohttp.ClientError:
            return ""

    def _url(self, path: str) -> str:
        return f"{self.valves.KVASIR_URL.rstrip('/')}{path}"


class _Options:
    """Per-user valves win over the admin ones, and both can defer to the service."""

    def __init__(self, valves: Any, user: dict[str, Any] | None) -> None:
        raw = (user or {}).get("valves")
        mine = raw if isinstance(raw, dict) else (raw.model_dump() if raw is not None else {})

        self.search_top_k = mine.get("SEARCH_TOP_K") or valves.SEARCH_TOP_K
        self.max_conv_turn = mine.get("MAX_CONV_TURN") or valves.MAX_CONV_TURN
        self.max_perspective = mine.get("MAX_PERSPECTIVE") or valves.MAX_PERSPECTIVE
        self.polish = _flag(mine.get("POLISH"), valves.POLISH)
        self.show_usage = _flag(mine.get("SHOW_USAGE"), valves.SHOW_USAGE)


def _flag(chosen: Any, fallback: bool) -> bool:
    return {"on": True, "off": False}.get(chosen, fallback)


class _Reasoning:
    """One collapsible reasoning block, written as the run reports.

    Open WebUI detects the tag pair in the stream, renders the content collapsed and times it, so
    nothing here needs to stamp a duration.
    """

    def __init__(self) -> None:
        self.used = False
        self._open = False
        self._stage: str | None = None
        self._detail: str | None = None

    def start(self) -> str:
        self.used = True
        self._open = True
        return "<think>\n"

    def line(self, stage: str, detail: str) -> str:
        # A wide article stage repeats itself, and a repeated line reads as a stall.
        if detail == self._detail:
            return ""
        self._detail = detail
        header = ""
        if stage != self._stage:
            self._stage = stage
            header = f"\n**{stage.replace('_', ' ')}**\n\n"
        return f"{header}- {detail}\n"

    def close(self) -> str:
        """Idempotent, so a failure can close the block without knowing whether it was open."""
        if not self._open:
            return ""
        self._open = False
        return "\n</think>\n\n"


class _Status:
    """Status events, plus a heartbeat so a long silent stage does not look like a hang."""

    def __init__(self, emitter: Emit) -> None:
        self._emitter = emitter
        self._started = time.monotonic()
        self._latest = "Starting"

    async def say(self, description: str) -> None:
        self._latest = description
        await self._emit(description)

    async def beat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self._emit(f"{self._latest} ({self.elapsed()})")

    async def done(self) -> None:
        await self._emit(f"Finished in {self.elapsed()}", done=True)

    def elapsed(self) -> str:
        return _duration(time.monotonic() - self._started)

    async def _emit(self, description: str, done: bool = False) -> None:
        if self._emitter:
            await self._emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )


class _Chat:
    """The events that decorate the chat rather than the message: title, tags, sources, toasts."""

    def __init__(self, emitter: Emit) -> None:
        self._emitter = emitter

    async def begin(self, body: dict[str, Any], kind: str, topic: str, title: bool) -> None:
        await self._emit({"type": "chat:tags", "data": {"tags": ["kvasir", kind]}})
        # Only the opening message: renaming the chat on every turn would fight the user.
        if title and not _has_answer(body):
            await self._emit({"type": "chat:title", "data": {"title": topic[:60]}})

    async def sources(self, citations: list[dict[str, Any]] | None) -> None:
        """One source event per citation, so Open WebUI renders the chips and the sources panel.

        The name carries the citation number, because the article's `[n]` markers are the only
        thing that maps a passage back to a source.
        """
        for citation in citations or []:
            url = citation["url"]
            name = citation["title"] or url
            await self._emit(
                {
                    "type": "source",
                    "data": {
                        "source": {"name": f"[{citation['index']}] {name}", "id": url},
                        "document": [citation.get("snippet") or ""],
                        "metadata": [{"source": url, "name": name, "url": url}],
                    },
                }
            )

    async def notify(self, level: str, content: str) -> None:
        await self._emit({"type": "notification", "data": {"type": level, "content": content}})

    async def _emit(self, event: dict[str, Any]) -> None:
        if self._emitter:
            await self._emitter(event)


class _ServiceError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"kvasir returned {status}: {detail[:500]}" if status != 200 else detail)


async def _confirm(call: Emit, title: str, message: str) -> bool:
    """Ask, and treat anything that is not an answer as consent rather than blocking the run."""
    if call is None:
        return True
    try:
        answer = await call({"type": "confirmation", "data": {"title": title, "message": message}})
    except Exception:
        return True
    if isinstance(answer, dict) and "error" in answer:
        return True
    return bool(answer)


async def _ask(call: Emit, message: str) -> str:
    if call is None:
        return ""
    try:
        answer = await call(
            {
                "type": "input",
                "data": {"title": "kvasir", "message": message, "placeholder": "A topic"},
            }
        )
    except Exception:
        return ""
    return answer.strip() if isinstance(answer, str) else ""


async def _events(response: aiohttp.ClientResponse) -> AsyncIterator[tuple[str, Any]]:
    """Yield (event, payload) pairs from an SSE body. A blank line terminates a frame."""
    event = ""
    async for raw in response.content:
        line = raw.decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            yield event, json.loads(line.removeprefix("data: "))


def _turn(turn: dict[str, Any]) -> str:
    role = turn.get("role") or "Round table"
    description = (turn.get("role_description") or "").strip()
    header = f"**{role}**" + (f" _{description}_" if description else "")

    parts = [header, "", turn.get("utterance", "")]
    if turn.get("mind_map_reorganised"):
        parts.append("\n_The mind map was reorganised._")
    return "\n".join(parts).strip()


def _spent(run: dict[str, Any]) -> str:
    usage = run.get("usage") or {}
    tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    cost = usage.get("cost", 0.0)
    # Zero is what a gateway that prices nothing reports, which is not the same as free.
    priced = f"${cost:.4f}" if cost else "cost not reported"

    stages = "\n".join(
        f"| {stage['name']} | {_duration((stage.get('ended_at') or 0) - stage['started_at'])} |"
        for stage in run.get("stages") or []
        if stage.get("ended_at")
    )
    roles = "\n".join(
        f"| {name} | {role['calls']} | {role['prompt_tokens'] + role['completion_tokens']:,} |"
        for name, role in (usage.get("by_role") or {}).items()
    )

    return (
        f"\n\n<details>\n<summary>Run {run.get('id')} · {tokens:,} tokens · {priced}</summary>\n\n"
        f"| stage | duration |\n| --- | --- |\n{stages}\n\n"
        f"| role | calls | tokens |\n| --- | --- | --- |\n{roles}\n\n"
        f"{usage.get('searches', 0)} searches, "
        f"{usage.get('embedding_tokens', 0):,} embedding tokens.\n\n</details>"
    )


def _duration(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}m {whole % 60}s"


def _has_answer(body: dict[str, Any]) -> bool:
    return any(message.get("role") == "assistant" for message in body.get("messages") or [])


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
