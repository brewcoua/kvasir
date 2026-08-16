"""The HTTP service. JSON in, SSE out, no authentication.

The service is reached only over a private network and sits behind the consumer's own policy layer.
Do not add an auth layer here, and do not bind to a public interface by default.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse

from kvasir.config import Settings, apply_environment
from kvasir.models import Error, ResearchRequest, ResearchResult
from kvasir.progress import ProgressStream
from kvasir.research import run_research
from kvasir.sse import HEADERS, MEDIA_TYPE, frame

READINESS_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger("kvasir")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Fail here rather than on the first request if the environment is unusable."""
    settings = Settings.from_env()
    apply_environment(settings)
    logging.basicConfig(level=settings.log_level)

    app.state.settings = settings
    # A run holds a thread for minutes, so saturation is rejected rather than queued. Acquiring
    # without blocking keeps this usable from the event loop.
    app.state.run_slots = threading.BoundedSemaphore(settings.max_concurrent_runs)
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
    """Run STORM and stream progress until the article is ready.

    Returns 429 rather than queueing when every run slot is taken, because a queued request would
    wait longer than any sensible client timeout.
    """
    settings: Settings = app.state.settings
    if not app.state.run_slots.acquire(blocking=False):
        return JSONResponse(
            {"message": "all run slots are busy, retry later"},
            status_code=429,
        )

    return StreamingResponse(
        _research_events(settings, request, app.state.run_slots),
        media_type=MEDIA_TYPE,
        headers=HEADERS,
    )


async def _research_events(
    settings: Settings, request: ResearchRequest, run_slots: threading.BoundedSemaphore
) -> AsyncIterator[str]:
    stream = ProgressStream()

    def work() -> ResearchResult:
        try:
            return run_research(settings, request, stream)
        finally:
            # Ends the iteration below even when the run raises.
            stream.close()

    try:
        # A disconnecting client does not stop the run. STORM offers no way to cancel one, and the
        # thread carries on to completion.
        worker = asyncio.create_task(asyncio.to_thread(work))
        async for event in stream:
            yield frame("progress", event)

        yield frame("done", await worker)
    except Exception as exc:
        logger.exception("research run failed for topic %r", request.topic)
        yield frame("error", Error(message=f"{type(exc).__name__}: {exc}"))
    finally:
        run_slots.release()
