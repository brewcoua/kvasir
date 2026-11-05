"""Starting runs, listing them, and following one live."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from kvasir.api.sse import HEADERS, MEDIA_TYPE, frame
from kvasir.api.streaming import streamed
from kvasir.config import ConfigError, Settings
from kvasir.methods import METHODS
from kvasir.research.sources import Sources, describe_urls, with_sources
from kvasir.runners import check_model_overrides
from kvasir.runs.registry import RunRegistry
from kvasir.runs.run import Run
from kvasir.schemas.requests import RunRequest
from kvasir.schemas.runs import LmCallContent, RunDetail, RunSummary, SearchContent

# How often a follower gets a fresh status while nothing else happens. Tokens are spent between
# progress events, and the polishing stage publishes none for minutes.
STATUS_INTERVAL_SECONDS = 2.0

router = APIRouter()


@router.post("/v1/runs")
async def start_run(http: Request, request: RunRequest) -> Response:
    """Run a method and stream progress until its result is ready."""
    settings: Settings = http.app.state.settings
    try:
        options = request.options
        check_model_overrides(
            settings,
            options.model_fast,
            options.model_strong,
            getattr(options, "model_reason", None),
        )
    except ConfigError as exc:
        return JSONResponse({"message": str(exc)}, status_code=400)
    method = METHODS[request.method]
    question = request.input.question
    return streamed(
        http.app.state,
        lambda stream: method.run(settings, request.input, request.options, stream),
        f"{method.name} {question!r}",
        method.name,
        question,
        method.stages(request.options),
    )


@router.get("/v1/runs")
async def list_runs(http: Request) -> list[RunSummary]:
    """Every run kept, newest first."""
    registry: RunRegistry = http.app.state.runs
    return registry.summaries()


@router.get("/v1/runs/{run_id}")
def run_detail(http: Request, run_id: str) -> RunDetail:
    registry: RunRegistry = http.app.state.runs
    return _sourced(registry.detail(run_id), http.app.state.sources)


@router.get("/v1/runs/{run_id}/calls/{call_id}")
async def call_content(http: Request, run_id: str, call_id: int) -> LmCallContent:
    """What one model call was sent and answered. Kept only with runs.keep_call_content."""
    registry: RunRegistry = http.app.state.runs
    return registry.content(run_id, call_id)


@router.get("/v1/runs/{run_id}/searches/{search_id}")
def search_content(http: Request, run_id: str, search_id: int) -> SearchContent:
    """What one search returned. Kept only with runs.keep_call_content."""
    registry: RunRegistry = http.app.state.runs
    content = registry.search_content(run_id, search_id)
    infos = describe_urls(http.app.state.sources, (result.url for result in content.results))
    return SearchContent(
        results=[
            result.model_copy(update={"source": infos.get(result.url)})
            for result in content.results
        ]
    )


@router.get("/v1/runs/{run_id}/events")
async def run_events(http: Request, run_id: str) -> Response:
    """Follow a run live, including one this client did not start.

    Opens with a snapshot, so a client attaching midway sees what it missed rather than only what
    happens next. Each progress event is followed by a status, as is every idle interval, so usage
    and stage timings stay current between snapshots.
    """
    registry: RunRegistry = http.app.state.runs
    record = registry.get(run_id)
    # A run only the store holds has ended, so its whole stream is the one snapshot.
    sources: Sources | None = http.app.state.sources
    events = (
        _watch(record, sources) if record is not None else _stored(registry.detail(run_id), sources)
    )
    return StreamingResponse(events, media_type=MEDIA_TYPE, headers=HEADERS)


def _sourced(detail: RunDetail, sources: Sources | None) -> RunDetail:
    """The detail with its result's citations marked by what is known of their sites now."""
    if detail.result is None:
        return detail
    return detail.model_copy(update={"result": with_sources(sources, detail.result)})


async def _stored(detail: RunDetail, sources: Sources | None) -> AsyncIterator[str]:
    yield frame("snapshot", await asyncio.to_thread(_sourced, detail, sources))


async def _watch(record: Run, sources: Sources | None) -> AsyncIterator[str]:
    # Registered before the snapshot is taken, so a run that finishes in between still delivers its
    # terminal event rather than leaving the client waiting on one that already happened.
    watcher = record.watch(heartbeat=STATUS_INTERVAL_SECONDS)
    try:
        detail = await asyncio.to_thread(_sourced, record.detail(), sources)
        yield frame("snapshot", detail)
        if detail.ended_at is None:
            async for event in watcher:
                if event is not None:
                    yield frame("progress", event)
                yield frame("status", record.status())
            yield frame("snapshot", await asyncio.to_thread(_sourced, record.detail(), sources))
    finally:
        record.unwatch(watcher)
