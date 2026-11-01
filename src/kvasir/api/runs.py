"""Starting runs, listing them, and following one live."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from kvasir.api.sse import HEADERS, MEDIA_TYPE, frame
from kvasir.api.streaming import streamed
from kvasir.article import run_article
from kvasir.config import Settings
from kvasir.methods.ledger.method import run_research
from kvasir.runs.progress import (
    ARTICLE,
    ASSESS,
    BACKGROUND,
    BRIEF,
    OUTLINE,
    POLISH,
    RESEARCH,
    VERIFY,
    WRITE,
)
from kvasir.runs.registry import RunRegistry
from kvasir.runs.run import Run
from kvasir.schemas.requests import ArticleRequest, Defaults, ResearchRequest
from kvasir.schemas.runs import LmCallContent, RunDetail, RunSummary

# How often a follower gets a fresh status while nothing else happens. Tokens are spent between
# progress events, and the polishing stage publishes none for minutes.
STATUS_INTERVAL_SECONDS = 2.0

router = APIRouter()


@router.post("/v1/article")
async def article(http: Request, request: ArticleRequest) -> Response:
    """Run STORM and stream progress until the article is ready."""
    settings: Settings = http.app.state.settings
    return streamed(
        http.app.state,
        lambda stream: run_article(settings, request, stream),
        f"article {request.topic!r}",
        "storm",
        request.topic,
        [RESEARCH, OUTLINE, ARTICLE] + ([POLISH] if request.do_polish_article else []),
    )


@router.post("/v1/research")
async def research(http: Request, request: ResearchRequest) -> Response:
    """Run the deep research pipeline and stream progress until the report is ready."""
    settings: Settings = http.app.state.settings
    return streamed(
        http.app.state,
        lambda stream: run_research(settings, request, stream),
        f"research {request.question!r}",
        "research",
        request.question,
        # Background and assess happen only for a ranking or a table, which the brief may choose.
        [BRIEF, RESEARCH, WRITE, VERIFY]
        if request.output == "report"
        else [BRIEF, BACKGROUND, RESEARCH, ASSESS, WRITE, VERIFY],
    )


@router.get("/v1/defaults")
async def defaults(http: Request) -> Defaults:
    """What a run's optional fields default to, as configured, for the runs page to show."""
    settings: Settings = http.app.state.settings
    return Defaults(
        model_fast=settings.model_fast,
        model_strong=settings.model_strong,
        max_searches=settings.research_max_searches,
        results_per_query=settings.research_results_per_query,
        max_attempts=settings.research_max_attempts,
        follow_ups=settings.research_follow_ups,
        max_depth=settings.research_max_depth,
        max_entities=settings.research_max_entities,
        max_criteria=settings.research_max_criteria,
        search_top_k=settings.search_top_k,
        max_conv_turn=settings.max_conv_turn,
        max_perspective=settings.max_perspective,
        do_polish_article=ArticleRequest.model_fields["do_polish_article"].default,
    )


@router.get("/v1/runs")
async def list_runs(http: Request) -> list[RunSummary]:
    """Every run kept, newest first."""
    registry: RunRegistry = http.app.state.runs
    return registry.summaries()


@router.get("/v1/runs/{run_id}")
async def run_detail(http: Request, run_id: str) -> RunDetail:
    registry: RunRegistry = http.app.state.runs
    return registry.detail(run_id)


@router.get("/v1/runs/{run_id}/calls/{call_id}")
async def call_content(http: Request, run_id: str, call_id: int) -> LmCallContent:
    """What one model call was sent and answered. Kept only with KVASIR_KEEP_CALL_CONTENT."""
    registry: RunRegistry = http.app.state.runs
    return registry.content(run_id, call_id)


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
    events = _watch(record) if record is not None else _stored(registry.detail(run_id))
    return StreamingResponse(events, media_type=MEDIA_TYPE, headers=HEADERS)


async def _stored(detail: RunDetail) -> AsyncIterator[str]:
    yield frame("snapshot", detail)


async def _watch(record: Run) -> AsyncIterator[str]:
    # Registered before the snapshot is taken, so a run that finishes in between still delivers its
    # terminal event rather than leaving the client waiting on one that already happened.
    watcher = record.watch(heartbeat=STATUS_INTERVAL_SECONDS)
    try:
        detail = record.detail()
        yield frame("snapshot", detail)
        if detail.ended_at is None:
            async for event in watcher:
                if event is not None:
                    yield frame("progress", event)
                yield frame("status", record.status())
            yield frame("snapshot", record.detail())
    finally:
        record.unwatch(watcher)
