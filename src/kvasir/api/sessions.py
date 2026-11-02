"""The Co-STORM round table: one session per chat, one operation per request."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from kvasir import conversation
from kvasir.api.streaming import streamed
from kvasir.config import ConfigError, Settings
from kvasir.runners import check_model_overrides
from kvasir.schemas.requests import SessionRequest, StepRequest
from kvasir.schemas.results import SessionInfo
from kvasir.sessions import SessionStore

router = APIRouter()


@router.post("/v1/session")
async def create_session(http: Request, request: SessionRequest) -> Response:
    """Create a session and warm start it. Streams, because warm start is a miniature STORM run."""
    settings: Settings = http.app.state.settings
    store: SessionStore = http.app.state.sessions
    try:
        check_model_overrides(settings, request.model_fast, request.model_strong)
    except ConfigError as exc:
        return JSONResponse({"message": str(exc)}, status_code=400)
    if store.exists(request.session_id):
        return JSONResponse({"message": "session already exists"}, status_code=409)

    return streamed(
        http.app.state,
        lambda stream: conversation.create(settings, store, request, stream),
        f"session {request.session_id}",
        "costorm",
        request.topic,
    )


@router.post("/v1/session/{session_id}/step")
async def step_session(http: Request, session_id: str, request: StepRequest) -> Response:
    """Take one turn. With an utterance the user speaks, without one an agent does."""
    settings: Settings = http.app.state.settings
    store: SessionStore = http.app.state.sessions
    return streamed(
        http.app.state,
        lambda stream: conversation.step(settings, store, session_id, request.utterance, stream),
        f"step {session_id}",
        "costorm",
        session_id,
    )


@router.post("/v1/session/{session_id}/report")
async def session_report(http: Request, session_id: str) -> Response:
    """Streams, because generating a report spends tokens and takes a slot like any other run."""
    settings: Settings = http.app.state.settings
    store: SessionStore = http.app.state.sessions
    return streamed(
        http.app.state,
        lambda stream: conversation.report(settings, store, session_id, stream),
        f"report {session_id}",
        "costorm",
        f"report {session_id}",
    )


@router.get("/v1/session/{session_id}")
async def session_info(http: Request, session_id: str) -> SessionInfo:
    return conversation.info(http.app.state.sessions, session_id)


@router.delete("/v1/session/{session_id}", status_code=204)
async def delete_session(http: Request, session_id: str) -> None:
    http.app.state.sessions.delete(session_id)
