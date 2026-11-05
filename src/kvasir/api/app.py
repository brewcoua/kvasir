"""The application: settings and shared state at startup, the page, health, and error responses.

The routes are in the modules beside this one. Each needs the token: see `kvasir.api.auth`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from kvasir import logs
from kvasir.api import auth, favicons, methods, pages, runs, sessions, sources, tasks
from kvasir.config import Settings, apply_environment
from kvasir.research import fetch
from kvasir.research.favicons import Favicons, FaviconStore
from kvasir.research.pages import PageStore
from kvasir.runners import build_sources
from kvasir.runs.registry import (
    CallContentNotFound,
    RunNotFound,
    RunRegistry,
    SearchContentNotFound,
)
from kvasir.runs.store import RunStore
from kvasir.sessions import SessionIdError, SessionNotFound, SessionStore
from kvasir.storm.runtime import configure_concurrency, configure_context, configure_fetch

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
    configure_context(settings.model.context_tokens)
    configure_fetch(
        settings.fetch.contact,
        settings.fetch.host_interval_seconds,
        settings.fetch.render,
        settings.fetch.stealth,
    )
    fetch.configure_cache(PageStore(settings.pages_db), settings.fetch.max_age_hours)
    logs.configure(settings)

    app.state.settings = settings
    # The database file is not made when favicons are off.
    app.state.favicons = (
        Favicons(settings.favicons, FaviconStore(settings.favicons_db))
        if settings.favicons.providers
        else None
    )
    # The runs' own instance, so a mark takes effect for a run in flight. None when off.
    app.state.sources = build_sources(settings)
    # A run holds a thread for minutes, so saturation is rejected rather than queued. Acquiring
    # without blocking keeps this usable from the event loop.
    app.state.run_slots = threading.BoundedSemaphore(settings.runs.max_concurrent)
    app.state.sessions = SessionStore(settings.sessions_dir, settings.sessions.ttl_hours)
    store = (
        RunStore(settings.runs_db, settings.runs.ttl_days, settings.runs.max)
        if settings.runs.store == "sqlite"
        else None
    )
    app.state.runs = RunRegistry(settings.runs.max, store, settings.runs.keep_call_content)
    # Once, here. Expiry needs no scheduler and no background task.
    app.state.sessions.sweep()
    if store is not None:
        store.sweep()
    logger.info(
        "kvasir ready, providers %s, searxng %s, %d concurrent run(s)",
        ", ".join(f"{name}={provider.api_base}" for name, provider in settings.providers.items()),
        settings.search.url,
        settings.runs.max_concurrent,
    )
    yield


app = FastAPI(title="kvasir", lifespan=lifespan)
app.include_router(auth.router)
for module in (runs, methods, pages, favicons, sources, tasks, sessions):
    app.include_router(module.router, dependencies=[Depends(auth.require_token)])


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
    """Liveness. Deliberately checks no dependency: a provider outage must not restart the pod."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, object]:
    """Readiness. Fails when SearXNG or any provider a model uses is unreachable.

    No check spends tokens. Listing models is free, and SearXNG is only asked for its root. A
    provider no model refers to is not probed, since no run needs it.
    """
    settings: Settings = app.state.settings
    refs = (
        settings.model.fast,
        settings.model.strong,
        settings.model.checker,
        settings.model.embedding,
    )
    names = sorted({ref.partition("/")[0] for ref in refs if ref is not None})
    *probed, searxng = await asyncio.gather(
        *(
            _reachable(
                f"{settings.providers[name].api_base}/models",
                headers={"Authorization": f"Bearer {settings.providers[name].api_key}"},
            )
            for name in names
        ),
        _reachable(settings.search.url),
    )
    providers = dict(zip(names, probed, strict=True))

    if not (all(providers.values()) and searxng):
        response.status_code = 503
    return {"providers": providers, "searxng": searxng}


async def _reachable(url: str, headers: dict[str, str] | None = None) -> bool:
    try:
        async with httpx.AsyncClient(timeout=READINESS_TIMEOUT_SECONDS) as client:
            result = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("readiness check failed for %s: %s", url, exc)
        return False
    return result.status_code < 500


@app.exception_handler(auth.Unauthorized)
async def _unauthorized(request: Request, exc: auth.Unauthorized) -> JSONResponse:
    return JSONResponse(
        {"message": "missing or wrong token"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.exception_handler(RunNotFound)
async def _run_not_found(request: Request, exc: RunNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no run {exc}"}, status_code=404)


@app.exception_handler(CallContentNotFound)
async def _call_content_not_found(request: Request, exc: CallContentNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no content kept for call {exc}"}, status_code=404)


@app.exception_handler(SearchContentNotFound)
async def _search_content_not_found(request: Request, exc: SearchContentNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no results kept for search {exc}"}, status_code=404)


@app.exception_handler(pages.PagesError)
async def _pages_error(request: Request, exc: pages.PagesError) -> JSONResponse:
    return JSONResponse({"message": str(exc)}, status_code=exc.status_code)


@app.exception_handler(sources.SourcesError)
async def _sources_error(request: Request, exc: sources.SourcesError) -> JSONResponse:
    return JSONResponse({"message": str(exc)}, status_code=exc.status_code)


@app.exception_handler(SessionNotFound)
async def _session_not_found(request: Request, exc: SessionNotFound) -> JSONResponse:
    return JSONResponse({"message": f"no session {exc}"}, status_code=404)


@app.exception_handler(SessionIdError)
async def _bad_session_id(request: Request, exc: SessionIdError) -> JSONResponse:
    return JSONResponse({"message": str(exc)}, status_code=400)
