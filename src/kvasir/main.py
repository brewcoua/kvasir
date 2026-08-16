"""The HTTP service. JSON in, SSE out, no authentication.

The service is reached only over a private network and sits behind the consumer's own policy layer.
Do not add an auth layer here, and do not bind to a public interface by default.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from kvasir import conversation, logs
from kvasir.config import Settings, apply_environment
from kvasir.models import (
    Error,
    Report,
    ResearchRequest,
    RunDetail,
    RunStarted,
    RunSummary,
    SessionInfo,
    SessionRequest,
    StepRequest,
)
from kvasir.progress import ProgressStream
from kvasir.research import run_research
from kvasir.runs import Run, RunNotFound, RunRegistry
from kvasir.sessions import SessionIdError, SessionNotFound, SessionStore
from kvasir.sse import HEADERS, MEDIA_TYPE, frame
from kvasir.storm.runtime import configure_cache, configure_concurrency

READINESS_TIMEOUT_SECONDS = 5.0
INDEX = Path(__file__).parent / "static" / "index.html"

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
    logs.configure(settings)

    app.state.settings = settings
    # A run holds a thread for minutes, so saturation is rejected rather than queued. Acquiring
    # without blocking keeps this usable from the event loop.
    app.state.run_slots = threading.BoundedSemaphore(settings.max_concurrent_runs)
    app.state.sessions = SessionStore(settings.sessions_dir, settings.session_ttl_hours)
    app.state.runs = RunRegistry()
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


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """The runs page. One file, no build step, and nothing loaded from another origin."""
    return FileResponse(INDEX, media_type="text/html")


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
        lambda stream: run_research(settings, request, stream),
        f"research {request.topic!r}",
        "storm",
        request.topic,
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
        "costorm",
        request.topic,
    )


@app.post("/v1/session/{session_id}/step")
async def step_session(session_id: str, request: StepRequest) -> Response:
    """Take one turn. With an utterance the user speaks, without one an agent does."""
    settings: Settings = app.state.settings
    store: SessionStore = app.state.sessions
    return _streamed(
        lambda stream: conversation.step(settings, store, session_id, request.utterance, stream),
        f"step {session_id}",
        "costorm",
        session_id,
    )


@app.post("/v1/session/{session_id}/report")
async def session_report(session_id: str) -> Report:
    """Not streamed, but still a run: generating a report spends tokens like any other stage."""
    record = app.state.runs.start("costorm", f"report {session_id}")

    def work() -> Report:
        with record.active():
            return conversation.report(app.state.settings, app.state.sessions, session_id)

    return await asyncio.to_thread(work)


@app.get("/v1/runs")
async def list_runs() -> list[RunSummary]:
    """Every run the process remembers, newest first."""
    return [record.summary() for record in app.state.runs.list()]


@app.get("/v1/runs/{run_id}")
async def run_detail(run_id: str) -> RunDetail:
    return _run(run_id).detail()


@app.get("/v1/runs/{run_id}/events")
async def run_events(run_id: str) -> Response:
    """Follow a run live, including one this client did not start.

    Opens with a snapshot, so a client attaching midway sees what it missed rather than only what
    happens next.
    """
    return StreamingResponse(_watch(_run(run_id)), media_type=MEDIA_TYPE, headers=HEADERS)


def _run(run_id: str) -> Run:
    registry: RunRegistry = app.state.runs
    record = registry.get(run_id)
    if record is None:
        raise RunNotFound(run_id)
    return record


async def _watch(record: Run) -> AsyncIterator[str]:
    # Registered before the snapshot is taken, so a run that finishes in between still delivers its
    # terminal event rather than leaving the client waiting on one that already happened.
    watcher = record.watch()
    try:
        detail = record.detail()
        yield frame("snapshot", detail)
        if detail.ended_at is None:
            async for event in watcher:
                yield frame("progress", event)
            yield frame("snapshot", record.detail())
    finally:
        record.unwatch(watcher)


@app.get("/v1/session/{session_id}")
async def session_info(session_id: str) -> SessionInfo:
    return conversation.info(app.state.sessions, session_id)


@app.delete("/v1/session/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    app.state.sessions.delete(session_id)


@app.exception_handler(RunNotFound)
async def _run_not_found(request: Request, exc: RunNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no run {exc}"}, status_code=404)


@app.exception_handler(SessionNotFound)
async def _session_not_found(request: Request, exc: SessionNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no session {exc}"}, status_code=404)


@app.exception_handler(SessionIdError)
async def _bad_session_id(request: Request, exc: SessionIdError) -> JSONResponse:
    return JSONResponse({"message": str(exc)}, status_code=400)


def _streamed(
    work: Callable[[ProgressStream], BaseModel], description: str, kind: str, topic: str
) -> Response:
    """Run blocking work in a thread, streaming progress until it produces a result.

    Returns 429 rather than queueing when every run slot is taken, because a queued request would
    wait longer than any sensible client timeout, and a refusal consumes no slot.
    """
    # Registered before the slot is claimed, so a refusal is a run that shows up as rejected rather
    # than a 429 with nothing behind it. Saturation is the case where the registry earns its keep.
    run = app.state.runs.start(kind, topic)
    if not app.state.run_slots.acquire(blocking=False):
        run.reject()
        logger.warning("rejected %s: every run slot is busy", description)
        return JSONResponse({"message": "all run slots are busy, retry later"}, status_code=429)

    return StreamingResponse(
        _events(work, description, run, app.state.run_slots),
        media_type=MEDIA_TYPE,
        headers=HEADERS,
    )


async def _events(
    work: Callable[[ProgressStream], BaseModel],
    description: str,
    record: Run,
    run_slots: threading.BoundedSemaphore,
) -> AsyncIterator[str]:
    stream = ProgressStream()

    def run() -> BaseModel:
        started = time.monotonic()
        try:
            # Entered on the worker thread, so the run identity is scoped to this run rather than
            # shared with the event loop, and the pipeline's pools inherit it with the context.
            with record.active():
                logger.info("started %s", description)
                return work(stream)
        finally:
            logger.info("finished %s in %.1fs", description, time.monotonic() - started)
            # Ends the iteration below even when the work raises.
            stream.close()

    try:
        # First, so a client can follow the run at /v1/runs/{id} even if it stops reading here.
        yield frame("run", RunStarted(run_id=record.id))
        # A disconnecting client does not stop the work. Upstream offers no way to cancel a run,
        # and the thread carries on to completion.
        worker = asyncio.create_task(asyncio.to_thread(run))
        async for event in stream:
            yield frame("progress", event)

        yield frame("done", await worker)
    except SessionNotFound as exc:
        yield frame("error", Error(message=f"no session {exc}"))
    except Exception as exc:
        # Raised on the event loop rather than in the run's thread, so the run is named explicitly.
        logger.exception(
            "%s failed", description, extra={"run_id": record.id, "run_kind": record.kind}
        )
        yield frame("error", Error(message=f"{type(exc).__name__}: {exc}"))
    finally:
        run_slots.release()
