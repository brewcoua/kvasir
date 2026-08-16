"""Drives the Co-STORM Pipe against a real running service over a real socket.

Only the Co-STORM engine itself is faked. Session persistence, routing, SSE framing and the two
step calls a spoken turn needs are all exercised for real.
"""

import importlib.util
import socket
import threading
from pathlib import Path

import pytest
import uvicorn

from kvasir import main
from kvasir.models import Citation, Report, SessionInfo, Turn

PIPE_PATH = Path(__file__).parents[1] / "openwebui" / "costorm.py"

INFO = SessionInfo(
    session_id="chat-1",
    topic="The Kvasir stone",
    turn_count=0,
    experts=["Petrologist", "Local historian"],
    updated_at=1.0,
)

AGENT_TURN = Turn(
    role="Petrologist",
    role_description="studies rock provenance",
    utterance="The source outcrop lies four hundred kilometres north [1].",
    utterance_type="Support",
    citations=[Citation(index=1, url="https://example.org/a", title="Provenance", snippet="s")],
    mind_map_reorganised=True,
)

USER_TURN = Turn(
    role="Guest",
    role_description="",
    utterance="How was it dated?",
    utterance_type="Original Question",
    citations=[],
    mind_map_reorganised=False,
)


def load_pipe():
    spec = importlib.util.spec_from_file_location("owui_costorm", PIPE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    for name, value in {
        "OPENAI_API_KEY": "key",
        "OPENAI_API_BASE": "https://gateway.example/v1",
        "KVASIR_MODEL_FAST": "openai/ollama/fast:cloud",
        "KVASIR_MODEL_STRONG": "openai/ollama/strong:cloud",
        "KVASIR_SEARXNG_URL": "http://searxng.example",
    }.items():
        monkeypatch.setenv(name, value)

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
def engine(service, monkeypatch):
    """Fakes the Co-STORM engine, recording the calls the pipe makes."""
    calls = []

    def create(settings, store, request, stream):
        stream.publish("warm_start", "gathering background")
        store.save(
            request.session_id,
            {
                "runner_argument": {"topic": request.topic},
                "conversation_history": [],
                "experts": [{"role_name": name} for name in INFO.experts],
            },
        )
        calls.append(("create", request.topic))
        return INFO

    def step(settings, store, session_id, utterance, stream):
        stream.publish("turn", "searching for sources")
        calls.append(("step", utterance))
        return USER_TURN if utterance else AGENT_TURN

    def report(settings, store, session_id):
        calls.append(("report", session_id))
        return Report(report="# Report\n\nFindings.", citations=AGENT_TURN.citations)

    monkeypatch.setattr(main.conversation, "create", create)
    monkeypatch.setattr(main.conversation, "step", step)
    monkeypatch.setattr(main.conversation, "report", report)
    return calls


@pytest.fixture
def pipe(service):
    module = load_pipe()
    instance = module.Pipe()
    instance.valves.KVASIR_URL = service
    return instance


def message(text):
    return {"messages": [{"role": "user", "content": text}]}


METADATA = {"chat_id": "chat-1"}


def test_it_exposes_one_model():
    assert load_pipe().Pipe().pipes() == [{"id": "costorm", "name": "Co-STORM"}]


@pytest.mark.asyncio
async def test_the_first_message_warm_starts_and_takes_a_turn(pipe, engine):
    answer = await pipe.pipe(message("The Kvasir stone"), None, METADATA)

    assert engine[0] == ("create", "The Kvasir stone")
    assert engine[1] == ("step", "")
    assert "Round table on **The Kvasir stone**" in answer
    assert "Petrologist, Local historian" in answer
    assert "four hundred kilometres north" in answer
    assert "_The mind map was reorganised._" in answer
    assert "1. [Provenance](https://example.org/a)" in answer


@pytest.mark.asyncio
async def test_warm_start_can_stop_without_taking_a_turn(pipe, engine):
    pipe.valves.ADVANCE_AFTER_WARM_START = False

    answer = await pipe.pipe(message("The Kvasir stone"), None, METADATA)

    assert [name for name, _ in engine] == ["create"]
    assert "Say `next` to advance" in answer


@pytest.mark.asyncio
async def test_the_session_is_keyed_by_chat_id_and_reused(pipe, engine):
    await pipe.pipe(message("The Kvasir stone"), None, METADATA)
    engine.clear()

    await pipe.pipe(message("next"), None, METADATA)

    # No second create: the existing session was found on disk.
    assert [name for name, _ in engine] == ["step"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["next", "NEXT", " next ", "/next"])
async def test_advance_takes_an_agent_turn_without_speaking(pipe, engine, command):
    await pipe.pipe(message("The Kvasir stone"), None, METADATA)
    engine.clear()

    answer = await pipe.pipe(message(command), None, METADATA)

    assert engine == [("step", "")]
    assert "Petrologist" in answer


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["report", "/report", "Report"])
async def test_report_returns_the_markdown_with_sources(pipe, engine, command):
    await pipe.pipe(message("The Kvasir stone"), None, METADATA)
    engine.clear()

    answer = await pipe.pipe(message(command), None, METADATA)

    assert [name for name, _ in engine] == ["report"]
    assert answer.startswith("# Report")
    assert "1. [Provenance](https://example.org/a)" in answer


@pytest.mark.asyncio
async def test_speaking_records_the_turn_then_asks_for_a_response(pipe, engine):
    await pipe.pipe(message("The Kvasir stone"), None, METADATA)
    engine.clear()

    answer = await pipe.pipe(message("How was it dated?"), None, METADATA)

    # A user turn is recorded rather than answered, so a second step produces the reply.
    assert engine == [("step", "How was it dated?"), ("step", "")]
    assert "four hundred kilometres north" in answer
    assert "_Recorded as Original Question._" in answer


@pytest.mark.asyncio
async def test_progress_reaches_the_event_emitter(pipe, engine):
    emitted = []

    async def emitter(event):
        emitted.append(event["data"]["description"])

    await pipe.pipe(message("The Kvasir stone"), emitter, METADATA)

    assert "warm_start: gathering background" in emitted
    assert emitted[-1].startswith("Finished in")


@pytest.mark.asyncio
async def test_a_failing_call_is_reported_not_raised(pipe, engine, monkeypatch):
    def failing(settings, store, request, stream):
        raise RuntimeError("searxng returned nothing")

    monkeypatch.setattr(main.conversation, "create", failing)

    assert "searxng returned nothing" in await pipe.pipe(message("x"), None, METADATA)


@pytest.mark.asyncio
async def test_a_missing_chat_id_is_reported(pipe, engine):
    answer = await pipe.pipe(message("The Kvasir stone"), None, {})

    assert "did not supply a chat id" in answer


@pytest.mark.asyncio
async def test_an_empty_message_asks_for_a_topic(pipe, engine):
    assert (
        await pipe.pipe({"messages": []}, None, METADATA) == "Send a topic to start a round table."
    )


@pytest.mark.asyncio
async def test_an_unreachable_service_is_reported_not_raised(pipe):
    pipe.valves.KVASIR_URL = "http://127.0.0.1:1"

    assert "Could not reach kvasir" in await pipe.pipe(message("x"), None, METADATA)
