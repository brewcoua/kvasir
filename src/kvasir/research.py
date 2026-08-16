"""Driving a STORM run."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from kvasir.config import Settings
from kvasir.models import ResearchRequest, ResearchResult
from kvasir.outputs import read_article, read_citations, read_outline
from kvasir.progress import ProgressStream, StormProgressHandler
from kvasir.runners import build_storm_runner


def run_research(
    settings: Settings,
    request: ResearchRequest,
    stream: ProgressStream,
) -> ResearchResult:
    """Run STORM to completion, blocking for minutes. Never call this on the event loop.

    The scratch directory is created and removed here rather than by the caller. Tying it to the
    lifetime of the response would delete it underneath this thread when a client disconnects.
    """
    with tempfile.TemporaryDirectory(prefix="kvasir-run-") as scratch:
        return _run(settings, request, stream, Path(scratch))


def _run(
    settings: Settings, request: ResearchRequest, stream: ProgressStream, scratch: Path
) -> ResearchResult:
    started = time.monotonic()

    runner = build_storm_runner(
        settings,
        scratch,
        search_top_k=request.search_top_k,
        max_conv_turn=request.max_conv_turn,
        max_perspective=request.max_perspective,
        model_fast=request.model_fast,
        model_strong=request.model_strong,
    )
    runner.run(
        topic=request.topic,
        callback_handler=StormProgressHandler(stream),
        do_polish_article=request.do_polish_article,
    )

    # run() records where it wrote, so the topic-to-directory rule is never reimplemented here.
    output = Path(runner.article_output_dir)
    return ResearchResult(
        article=read_article(output),
        outline=read_outline(output),
        citations=read_citations(output),
        duration_seconds=round(time.monotonic() - started, 1),
    )
