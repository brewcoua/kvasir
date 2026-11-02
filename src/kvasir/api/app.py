"""The application: settings and shared state at startup, the page, health, and error responses.

The routes are in the modules beside this one. See `kvasir.api` on why there is no authentication.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from kvasir import logs
from kvasir.api import methods, runs, sessions, tasks
from kvasir.config import Settings, apply_environment
from kvasir.runs.registry import (
    CallContentNotFound,
    RunNotFound,
    RunRegistry,
    SearchContentNotFound,
)
from kvasir.runs.store import RunStore
from kvasir.sessions import SessionIdError, SessionNotFound, SessionStore
from kvasir.storm.runtime import configure_concurrency, configure_context

READINESS_TIMEOUT_SECONDS = 5.0
# Built from web/ by `pnpm build`, which the image does and a checkout has to.
STATIC = Path(__file__).parents[1] / "static"
INDEX = STATIC / "index.html"

logger = logging.getLogger("kvasir")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Fail here rather than on the first request if the environment is unusable."""
    settings = Settings.from_env()
    apply_environment()
    configure_concurrency(settings.max_threads)
    configure_context(settings.context_tokens)
    logs.configure(settings)

    app.state.settings = settings
    # A run holds a thread for minutes, so saturation is rejected rather than queued. Acquiring
    # without blocking keeps this usable from the event loop.
    app.state.run_slots = threading.BoundedSemaphore(settings.max_concurrent_runs)
    app.state.sessions = SessionStore(settings.sessions_dir, settings.session_ttl_hours)
    store = (
        RunStore(settings.runs_db, settings.run_ttl_days, settings.max_runs)
        if settings.run_store == "sqlite"
        else None
    )
    app.state.runs = RunRegistry(settings.max_runs, store, settings.keep_call_content)
    # Once, here. Expiry needs no scheduler and no background task.
    app.state.sessions.sweep()
    if store is not None:
        store.sweep()
    logger.info(
        "kvasir ready, gateway %s, searxng %s, %d concurrent run(s)",
        settings.openai_api_base,
        settings.searxng_url,
        settings.max_concurrent_runs,
    )
    yield


app = FastAPI(title="kvasir", lifespan=lifespan)
for module in (runs, methods, tasks, sessions):
    app.include_router(module.router)


@app.get("/", include_in_schema=False)
async def index() -> Response:
    """The runs page, built from web/. Nothing it loads comes from another origin."""
    if not INDEX.is_file():
        return PlainTextResponse("The runs page is not built. Run `pnpm build` in web/.", 503)
    return FileResponse(INDEX, media_type="text/html")


# Not checked at import, so the API still starts from a checkout where the page is not built.
app.mount("/assets", StaticFiles(directory=STATIC / "assets", check_dir=False), name="assets")


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


@app.exception_handler(RunNotFound)
async def _run_not_found(request: Request, exc: RunNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no run {exc}"}, status_code=404)


@app.exception_handler(CallContentNotFound)
async def _call_content_not_found(request: Request, exc: CallContentNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no content kept for call {exc}"}, status_code=404)


@app.exception_handler(SearchContentNotFound)
async def _search_content_not_found(request: Request, exc: SearchContentNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no results kept for search {exc}"}, status_code=404)


@app.exception_handler(SessionNotFound)
async def _session_not_found(request: Request, exc: SessionNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no session {exc}"}, status_code=404)


@app.exception_handler(SessionIdError)
async def _bad_session_id(request: Request, exc: SessionIdError) -> JSONResponse:
    return JSONResponse({"message": str(exc)}, status_code=400)
