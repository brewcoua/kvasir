"""Driving a STORM run."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from kvasir.config import Settings
from kvasir.methods.base import Method
from kvasir.outputs import read_article, read_citations, read_outline
from kvasir.runners import build_storm_runner
from kvasir.runs.progress import (
    ARTICLE,
    OUTLINE,
    POLISH,
    RESEARCH,
    ProgressStream,
    StormProgressHandler,
)
from kvasir.schemas.requests import Input, StormOptions
from kvasir.schemas.results import ArticleResult


def run_article(
    settings: Settings,
    input: Input,
    options: StormOptions,
    stream: ProgressStream,
) -> ArticleResult:
    """Run STORM to completion, blocking for minutes. Never call this on the event loop.

    The scratch directory is created and removed here rather than by the caller. Tying it to the
    lifetime of the response would delete it underneath this thread when a client disconnects.
    """
    with tempfile.TemporaryDirectory(prefix="kvasir-run-") as scratch:
        return _run(settings, input.question, options, stream, Path(scratch))


def _run(
    settings: Settings, topic: str, options: StormOptions, stream: ProgressStream, scratch: Path
) -> ArticleResult:
    started = time.monotonic()

    runner = build_storm_runner(
        settings,
        scratch,
        search_top_k=options.search_top_k,
        max_conv_turn=options.max_conv_turn,
        max_perspective=options.max_perspective,
        model_fast=options.model_fast,
        model_strong=options.model_strong,
    )
    runner.run(
        topic=topic,
        callback_handler=StormProgressHandler(stream, runner.args.max_conv_turn),
        do_polish_article=options.do_polish_article,
    )

    # run() records where it wrote, so the topic-to-directory rule is never reimplemented here.
    output = Path(runner.article_output_dir)
    return ArticleResult(
        article=read_article(output),
        outline=read_outline(output),
        citations=read_citations(output),
        duration_seconds=round(time.monotonic() - started, 1),
    )


def _stages(options: StormOptions) -> list[str]:
    return [RESEARCH, OUTLINE, ARTICLE] + ([POLISH] if options.do_polish_article else [])


def _defaults(settings: Settings) -> dict[str, Any]:
    return {
        "search_top_k": settings.search_top_k,
        "max_conv_turn": settings.max_conv_turn,
        "max_perspective": settings.max_perspective,
        "model_fast": settings.model_fast,
        "model_strong": settings.model_strong,
    }


STORM = Method(
    name="storm",
    Options=StormOptions,
    clarifies=False,
    stages=_stages,
    defaults=_defaults,
    run=run_article,
)
