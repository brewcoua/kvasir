"""One fast-model call each, answered without registering a run or claiming a slot."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from kvasir.config import Settings
from kvasir.research.clarify import clarify
from kvasir.runners import TASK_RETRY_SECONDS, build_task_model
from kvasir.schemas.requests import ClarifyRequest, ClarifyResponse, TaskRequest, TaskResponse
from kvasir.storm.lm import retry_budget

router = APIRouter()


@router.post("/v1/clarify")
async def clarify_question(http: Request, request: ClarifyRequest) -> ClarifyResponse:
    """Questions to ask before researching, or none. One fast-model call.

    Like `/v1/tasks`, no run is registered and no slot is claimed: the user is still deciding
    whether to start one.
    """
    settings: Settings = http.app.state.settings
    with retry_budget(TASK_RETRY_SECONDS):
        questions = await asyncio.to_thread(clarify, settings, request.question)
    return ClarifyResponse(questions=questions)


@router.post("/v1/tasks")
async def task(http: Request, request: TaskRequest) -> TaskResponse:
    """One short completion on the fast model, for a client's own housekeeping.

    Open WebUI generates a chat title, tags and follow-up prompts by asking whichever model the
    chat is using, which for a kvasir chat is the pipe. Those are not research: sent to
    `/v1/runs` they each take a run slot, and with one slot they collide with the very run they
    are summarising. Deliberately not `streamed` — no run is registered and no slot is claimed.
    """
    settings: Settings = http.app.state.settings
    model = build_task_model(settings)
    # Blocking, like every other way this service reaches the gateway. The budget is copied into
    # the thread with the rest of the context.
    with retry_budget(TASK_RETRY_SECONDS):
        replies = await asyncio.to_thread(model, messages=request.messages)
    return TaskResponse(content=replies[0] if replies else "")
