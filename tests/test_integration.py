"""End-to-end runs against a real gateway and a real SearXNG.

Deselected by default through the marker expression in pyproject.toml, because they need
credentials, spend tokens and take minutes. Run them with:

    uv run pytest -m integration

They run on the service's own configuration: the environment it starts with, and the settings
stored in kvasir.db under KVASIR_DATA_DIR. The models and the search URL must be set there.

The SearXNG instance must have the JSON output format enabled. One serving HTML only produces an
empty result set rather than an error, which surfaces here as an article with no citations.
"""

import asyncio

import pytest

from kvasir.config import ConfigError, Settings, apply_environment
from kvasir.methods.storm.method import run_article
from kvasir.runs.progress import ProgressStream
from kvasir.schemas.requests import Input, StormOptions

pytestmark = pytest.mark.integration

TOPIC = "The Antikythera mechanism"


@pytest.fixture(scope="module")
def settings():
    try:
        resolved = Settings.load()
    except ConfigError as exc:
        pytest.skip(f"not configured: {exc}")
    if resolved.missing:
        pytest.skip(f"not configured: {', '.join(resolved.missing)}")
    apply_environment()
    return resolved


@pytest.fixture
async def stream():
    """Constructed inside a test, because it binds to the running event loop."""
    return ProgressStream()


@pytest.mark.asyncio
async def test_a_short_storm_run_produces_a_cited_article(settings, stream):
    """Deliberately the smallest run that still exercises every stage."""
    options = StormOptions(max_conv_turn=1, max_perspective=1, search_top_k=3)

    result = await _in_thread(run_article, settings, Input(question=TOPIC), options, stream)

    assert result.article.strip(), "the article is empty"
    assert result.outline.strip(), "the outline is empty"
    assert result.citations, "no citations, which usually means SearXNG is not serving JSON"
    assert result.citations[0].url.startswith("http")
    assert result.duration_seconds > 0


async def _in_thread(function, *args):
    """Every call under test is blocking, and none may run on the event loop."""
    return await asyncio.to_thread(function, *args)
