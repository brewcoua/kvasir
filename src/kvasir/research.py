"""Driving a STORM run stage by stage.

`run()` accepts one flag per stage and reloads whatever the previous stage left on disk, which is
what makes staged invocation the supported way to use it. Calling it once per stage costs a little
JSON parsing between stages and buys exact stage boundaries, because upstream's callbacks stop
after outline refinement and say nothing about the two longest stages. See docs/upstream-notes.md.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from kvasir.config import Settings
from kvasir.models import ResearchRequest, ResearchResult
from kvasir.outputs import read_article, read_citations, read_outline
from kvasir.progress import ARTICLE, POLISH, ProgressStream, StormProgressHandler
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
    handler = StormProgressHandler(stream)

    for stage in ("do_research", "do_generate_outline"):
        runner.run(topic=request.topic, callback_handler=handler, **_only(stage))

    # Upstream is silent from here on, so the remaining stages are announced before they start
    # rather than reported as they happen.
    stream.publish(ARTICLE, "writing sections with citations")
    runner.run(topic=request.topic, callback_handler=handler, **_only("do_generate_article"))

    if request.do_polish_article:
        stream.publish(POLISH, "polishing the article")
        runner.run(topic=request.topic, callback_handler=handler, **_only("do_polish_article"))

    runner.post_run()

    # run() records where it wrote, so the topic-to-directory rule is never reimplemented here.
    output = Path(runner.article_output_dir)
    return ResearchResult(
        article=read_article(output),
        outline=read_outline(output),
        citations=read_citations(output),
        duration_seconds=round(time.monotonic() - started, 1),
    )


_STAGE_FLAGS = (
    "do_research",
    "do_generate_outline",
    "do_generate_article",
    "do_polish_article",
)


def _only(stage: str) -> dict[str, bool]:
    """Flags enabling exactly one stage. run() asserts that at least one is set."""
    return {flag: flag == stage for flag in _STAGE_FLAGS}
