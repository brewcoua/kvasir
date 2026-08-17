"""End-to-end runs against a real gateway and a real SearXNG.

Deselected by default through the marker expression in pyproject.toml, because they need
credentials, spend tokens and take minutes. Run them with:

    uv run pytest -m integration

Required environment, the same names the service itself reads:

    OPENAI_API_KEY, OPENAI_API_BASE, KVASIR_MODEL_FAST, KVASIR_MODEL_STRONG, KVASIR_SEARXNG_URL

The SearXNG instance must have the JSON output format enabled. One serving HTML only produces an
empty result set rather than an error, which surfaces here as an article with no citations.
"""

import asyncio
import os

import pytest

from kvasir.config import Settings, apply_environment
from kvasir.conversation import create, report, step
from kvasir.models import ResearchRequest, SessionRequest
from kvasir.progress import ProgressStream
from kvasir.research import run_research
from kvasir.sessions import SessionStore

pytestmark = pytest.mark.integration

REQUIRED = (
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "KVASIR_MODEL_FAST",
    "KVASIR_MODEL_STRONG",
    "KVASIR_SEARXNG_URL",
)

TOPIC = "The Antikythera mechanism"


@pytest.fixture(scope="module")
def settings():
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        pytest.skip(f"not configured: {', '.join(missing)}")

    resolved = Settings.from_env()
    apply_environment(resolved)
    return resolved


@pytest.fixture
async def stream():
    """Constructed inside a test, because it binds to the running event loop."""
    return ProgressStream()


@pytest.mark.asyncio
async def test_a_short_storm_run_produces_a_cited_article(settings, stream):
    """Deliberately the smallest run that still exercises every stage."""
    request = ResearchRequest(topic=TOPIC, max_conv_turn=1, max_perspective=1, search_top_k=3)

    result = await _in_thread(run_research, settings, request, stream)

    assert result.article.strip(), "the article is empty"
    assert result.outline.strip(), "the outline is empty"
    assert result.citations, "no citations, which usually means SearXNG is not serving JSON"
    assert result.citations[0].url.startswith("http")
    assert result.duration_seconds > 0


@pytest.mark.asyncio
async def test_a_costorm_session_survives_being_reloaded(settings, tmp_path, stream):
    """The whole session design rests on state surviving a restart, so nothing is kept in memory.

    Each call below loads the session from disk and saves it back, which is exactly what a restart
    between requests would do.
    """
    store = SessionStore(tmp_path / "sessions", ttl_hours=1)
    request = SessionRequest(session_id="integration-1", topic=TOPIC)

    info = await _in_thread(create, settings, store, request, stream)
    assert info.experts, "warm start produced no experts"

    agent_turn = await _in_thread(step, settings, store, "integration-1", "", stream)
    assert agent_turn.utterance.strip(), "the agent turn is empty"

    user_turn = await _in_thread(
        step, settings, store, "integration-1", "How was it dated?", stream
    )
    assert user_turn.utterance.strip()

    # A fresh store, standing in for a process that restarted between turns.
    reloaded = SessionStore(tmp_path / "sessions", ttl_hours=1)
    final = await _in_thread(report, settings, reloaded, "integration-1", stream)

    assert final.report.strip(), "the report is empty"
    assert "#" in final.report, "the report carries no markdown headings"


async def _in_thread(function, *args):
    """Every call under test is blocking, and none may run on the event loop."""
    return await asyncio.to_thread(function, *args)
