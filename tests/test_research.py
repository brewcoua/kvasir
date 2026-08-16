"""The run is driven by one call to STORM, not one call per stage.

Staged invocation used to be the only way to know where a run had got to, because upstream stopped
calling back after outline refinement. The fork reports article generation and polishing, so the
stages are no longer recovered by slicing the run up and reloading each stage's output from disk.
"""

from __future__ import annotations

import pytest

from kvasir import research
from kvasir.models import ResearchRequest
from kvasir.progress import ARTICLE, POLISH, ProgressStream


class _Runner:
    """Stands in for STORMWikiRunner, writing what run() promises to leave on disk."""

    def __init__(self, output_dir):
        self.article_output_dir = str(output_dir)
        self.calls = []
        self.post_run_calls = 0
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "storm_gen_article_polished.txt").write_text("# summary\ntext")
        (output_dir / "storm_gen_outline.txt").write_text("# History")
        (output_dir / "url_to_info.json").write_text(
            '{"url_to_unified_index": {}, "url_to_info": {}}'
        )

    def run(self, topic, callback_handler=None, **flags):
        self.calls.append(flags)
        callback_handler.on_article_generation_start(sections=["History"])
        callback_handler.on_section_generation_start(section="History")
        callback_handler.on_section_generation_end(section="History")
        callback_handler.on_article_generation_end()
        if flags.get("do_polish_article", True):
            callback_handler.on_polish_start()
            callback_handler.on_polish_end()

    def post_run(self):
        self.post_run_calls += 1


@pytest.fixture
def runner(monkeypatch, tmp_path):
    built = _Runner(tmp_path / "out")
    monkeypatch.setattr(research, "build_storm_runner", lambda *args, **kwargs: built)
    return built


async def _run(request, stream):
    return research.run_research(object(), request, stream)


@pytest.mark.asyncio
async def test_the_pipeline_is_driven_by_a_single_run_call(runner):
    stream = ProgressStream()

    result = await _run(ResearchRequest(topic="Antikythera"), stream)
    stream.close()

    assert runner.calls == [{"do_polish_article": True}]
    assert result.article == "# summary\ntext"


@pytest.mark.asyncio
async def test_post_run_is_left_to_the_engine(runner):
    """run() calls it now, so calling it again here would truncate the LM call history."""
    stream = ProgressStream()

    await _run(ResearchRequest(topic="Antikythera"), stream)
    stream.close()

    assert runner.post_run_calls == 0


@pytest.mark.asyncio
async def test_sections_and_polishing_are_reported_as_they_happen(runner):
    stream = ProgressStream()

    await _run(ResearchRequest(topic="Antikythera"), stream)
    stream.close()

    events = [(event.stage, event.detail) async for event in stream]
    assert (ARTICLE, "writing History") in events
    assert (ARTICLE, "finished History (1/1)") in events
    assert (POLISH, "polished the article") in events


@pytest.mark.asyncio
async def test_polishing_is_skipped_when_the_request_disables_it(runner):
    stream = ProgressStream()

    await _run(ResearchRequest(topic="Antikythera", do_polish_article=False), stream)
    stream.close()

    assert runner.calls == [{"do_polish_article": False}]
    events = [event.stage async for event in stream]
    assert POLISH not in events
