"""The HTTP service. JSON in, SSE out, no authentication.

The service is reached only over a private network and sits behind the consumer's own policy layer.
Do not add an auth layer here, and do not bind to a public interface by default.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from kvasir import conversation
from kvasir.config import Settings, apply_environment
from kvasir.models import (
    Error,
    Report,
    ResearchRequest,
    SessionInfo,
    SessionRequest,
    StepRequest,
)
from kvasir.progress import ProgressStream
from kvasir.research import run_research
from kvasir.sessions import SessionIdError, SessionNotFound, SessionStore
from kvasir.sse import HEADERS, MEDIA_TYPE, frame
from kvasir.storm.runtime import configure_cache, configure_concurrency

READINESS_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger("kvasir")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Fail here rather than on the first request if the environment is unusable."""
    settings = Settings.from_env()
    apply_environment(settings)
    # Nothing in the fork opens a cache by itself, so this is the only call that decides where
    # model and embedding responses are cached.
    configure_cache(settings.cache_dir)
    configure_concurrency(settings.max_threads)
    logging.basicConfig(level=settings.log_level)

    app.state.settings = settings
    # A run holds a thread for minutes, so saturation is rejected rather than queued. Acquiring
    # without blocking keeps this usable from the event loop.
    app.state.run_slots = threading.BoundedSemaphore(settings.max_concurrent_runs)
    app.state.sessions = SessionStore(settings.sessions_dir, settings.session_ttl_hours)
    # Once, here. Expiry needs no scheduler and no background task.
    app.state.sessions.sweep()
    logger.info(
        "kvasir ready, gateway %s, searxng %s, %d concurrent run(s)",
        settings.openai_api_base,
        settings.searxng_url,
        settings.max_concurrent_runs,
    )
    yield


app = FastAPI(title="kvasir", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness. Deliberately checks no dependency: a gateway outage must not restart the pod."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, object]:
    """Readiness. Fails when either dependency is unreachable, since a run needs both.

    Neither check spends tokens. Listing models is free, and SearXNG is only asked for its root.
    """
    settings: Settings = app.state.settings
    checks = await asyncio.gather(
        _reachable(
            f"{settings.openai_api_base}/models",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        ),
        _reachable(settings.searxng_url),
    )
    gateway, searxng = checks

    if not (gateway and searxng):
        response.status_code = 503
    return {"gateway": gateway, "searxng": searxng}


async def _reachable(url: str, headers: dict[str, str] | None = None) -> bool:
    try:
        async with httpx.AsyncClient(timeout=READINESS_TIMEOUT_SECONDS) as client:
            result = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("readiness check failed for %s: %s", url, exc)
        return False
    return result.status_code < 500


@app.post("/v1/research")
async def research(request: ResearchRequest) -> Response:
    """Run STORM and stream progress until the article is ready."""
    settings: Settings = app.state.settings
    return _streamed(
        lambda stream: run_research(settings, request, stream), f"research {request.topic!r}"
    )


@app.post("/v1/session")
async def create_session(request: SessionRequest) -> Response:
    """Create a session and warm start it. Streams, because warm start is a miniature STORM run."""
    settings: Settings = app.state.settings
    store: SessionStore = app.state.sessions
    if store.exists(request.session_id):
        return JSONResponse({"message": "session already exists"}, status_code=409)

    return _streamed(
        lambda stream: conversation.create(settings, store, request, stream),
        f"session {request.session_id}",
    )


@app.post("/v1/session/{session_id}/step")
async def step_session(session_id: str, request: StepRequest) -> Response:
    """Take one turn. With an utterance the user speaks, without one an agent does."""
    settings: Settings = app.state.settings
    store: SessionStore = app.state.sessions
    return _streamed(
        lambda stream: conversation.step(settings, store, session_id, request.utterance, stream),
        f"step {session_id}",
    )


@app.post("/v1/session/{session_id}/report")
async def session_report(session_id: str) -> Report:
    return await asyncio.to_thread(
        conversation.report, app.state.settings, app.state.sessions, session_id
    )


@app.get("/v1/session/{session_id}")
async def session_info(session_id: str) -> SessionInfo:
    return conversation.info(app.state.sessions, session_id)


@app.delete("/v1/session/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    app.state.sessions.delete(session_id)


@app.exception_handler(SessionNotFound)
async def _session_not_found(request: Request, exc: SessionNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no session {exc}"}, status_code=404)


@app.exception_handler(SessionIdError)
async def _bad_session_id(request: Request, exc: SessionIdError) -> JSONResponse:
    return JSONResponse({"message": str(exc)}, status_code=400)


def _streamed(work: Callable[[ProgressStream], BaseModel], description: str) -> Response:
    """Run blocking work in a thread, streaming progress until it produces a result.

    Returns 429 rather than queueing when every run slot is taken, because a queued request would
    wait longer than any sensible client timeout, and a refusal consumes no slot.
    """
    if not app.state.run_slots.acquire(blocking=False):
        return JSONResponse({"message": "all run slots are busy, retry later"}, status_code=429)

    return StreamingResponse(
        _events(work, description, app.state.run_slots),
        media_type=MEDIA_TYPE,
        headers=HEADERS,
    )


async def _events(
    work: Callable[[ProgressStream], BaseModel],
    description: str,
    run_slots: threading.BoundedSemaphore,
) -> AsyncIterator[str]:
    stream = ProgressStream()

    def run() -> BaseModel:
        try:
            return work(stream)
        finally:
            # Ends the iteration below even when the work raises.
            stream.close()

    try:
        # A disconnecting client does not stop the work. Upstream offers no way to cancel a run,
        # and the thread carries on to completion.
        worker = asyncio.create_task(asyncio.to_thread(run))
        async for event in stream:
            yield frame("progress", event)

        yield frame("done", await worker)
    except SessionNotFound as exc:
        yield frame("error", Error(message=f"no session {exc}"))
    except Exception as exc:
        logger.exception("%s failed", description)
        yield frame("error", Error(message=f"{type(exc).__name__}: {exc}"))
    finally:
        run_slots.release()
