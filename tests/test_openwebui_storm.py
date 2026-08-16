"""Drives the Open WebUI Pipe against a real running service over a real socket.

The Pipe is pasted into Open WebUI by hand and cannot be imported as a package, so it is loaded
from its path. Running it against uvicorn rather than a mock is the point: SSE parsing and chunk
boundaries are exactly what a mocked transport would fail to exercise.
"""

import importlib.util
import socket
import threading
from pathlib import Path

import pytest
import uvicorn

from kvasir import main
from kvasir.models import Citation, ResearchResult

PIPE_PATH = Path(__file__).parents[1] / "openwebui" / "storm.py"

RESULT = ResearchResult(
    article="# Discovery\n\nThe stone was catalogued in 1783 [1].",
    outline="# Discovery",
    citations=[
        Citation(index=1, url="https://example.org/a", title="Parish survey", snippet="s"),
        Citation(index=2, url="https://example.org/b", title="", snippet="s"),
    ],
    duration_seconds=125.0,
)


def load_pipe():
    spec = importlib.util.spec_from_file_location("owui_storm", PIPE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service(monkeypatch, tmp_path):
    """A real kvasir on a real port, with the STORM run itself faked."""
    # Startup creates the sessions directory, and the default /data is not writable here.
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    for name, value in {
        "OPENAI_API_KEY": "key",
        "OPENAI_API_BASE": "https://gateway.example/v1",
        "KVASIR_MODEL_FAST": "openai/ollama/fast:cloud",
        "KVASIR_MODEL_STRONG": "openai/ollama/strong:cloud",
        "KVASIR_SEARXNG_URL": "http://searxng.example",
    }.items():
        monkeypatch.setenv(name, value)

    def fake_run(settings, request, stream):
        stream.publish("research", "asking questions")
        stream.publish("polish", "polishing the article")
        return RESULT

    monkeypatch.setattr(main, "run_research", fake_run)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("the test server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def pipe(service):
    module = load_pipe()
    instance = module.Pipe()
    instance.valves.KVASIR_URL = service
    return instance


def message(text):
    return {"messages": [{"role": "user", "content": text}]}


def test_it_exposes_one_model():
    assert load_pipe().Pipe().pipes() == [{"id": "storm", "name": "STORM"}]


@pytest.mark.asyncio
async def test_it_returns_the_article_with_references(pipe):
    emitted = []

    async def emitter(event):
        emitted.append(event["data"]["description"])

    answer = await pipe.pipe(message("The Kvasir stone"), emitter)

    assert answer.startswith("# Discovery")
    assert "## References" in answer
    assert "1. [Parish survey](https://example.org/a)" in answer
    # A source with no title falls back to its URL rather than rendering an empty link.
    assert "2. [https://example.org/b](https://example.org/b)" in answer
    assert "2m 5s" in answer

    assert "research: asking questions" in emitted
    assert "polish: polishing the article" in emitted
    assert emitted[-1].startswith("Finished in")


@pytest.mark.asyncio
async def test_it_reports_a_failed_run(pipe, monkeypatch):
    def failing_run(settings, request, stream):
        raise RuntimeError("searxng returned nothing")

    monkeypatch.setattr(main, "run_research", failing_run)

    assert "searxng returned nothing" in await pipe.pipe(message("x"))


@pytest.mark.asyncio
async def test_it_reports_saturation_rather_than_hanging(pipe):
    assert main.app.state.run_slots.acquire(blocking=False)
    try:
        answer = await pipe.pipe(message("x"))
    finally:
        main.app.state.run_slots.release()

    assert "429" in answer and "busy" in answer


@pytest.mark.asyncio
async def test_an_unreachable_service_is_reported_not_raised(pipe):
    pipe.valves.KVASIR_URL = "http://127.0.0.1:1"

    assert "Could not reach kvasir" in await pipe.pipe(message("x"))


@pytest.mark.asyncio
async def test_it_asks_for_a_topic_when_there_is_none(pipe):
    assert await pipe.pipe({"messages": []}) == "Send a topic to research."


@pytest.mark.asyncio
async def test_only_the_last_user_message_is_researched(pipe, monkeypatch):
    sent = {}

    def capture(settings, request, stream):
        sent["topic"] = request.topic
        return RESULT

    monkeypatch.setattr(main, "run_research", capture)
    await pipe.pipe(
        {
            "messages": [
                {"role": "user", "content": "an earlier topic"},
                {"role": "assistant", "content": "an article"},
                {"role": "user", "content": "the real topic"},
            ]
        }
    )

    assert sent["topic"] == "the real topic"


@pytest.mark.asyncio
async def test_unset_valves_leave_the_defaults_to_the_service(pipe, monkeypatch):
    sent = {}

    def capture(settings, request, stream):
        sent["request"] = request
        return RESULT

    monkeypatch.setattr(main, "run_research", capture)
    await pipe.pipe(message("x"))

    request = sent["request"]
    assert request.search_top_k is None
    assert request.max_conv_turn is None
    assert request.model_fast is None
