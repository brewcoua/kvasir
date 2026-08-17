"""Drives the Open WebUI Pipe against a real running service over a real socket.

The Pipe is pasted into Open WebUI by hand and cannot be imported as a package, so it is loaded
from its path. Running it against uvicorn rather than a mock is the point: SSE parsing and chunk
boundaries are exactly what a mocked transport would fail to exercise. Only the two engines are
faked; session persistence, routing, the run registry and SSE framing are all real.
"""

import importlib.util
import socket
import threading
from pathlib import Path

import pytest
import uvicorn

from kvasir import main
from kvasir.models import Citation, Report, ResearchResult, SessionInfo, Turn

PIPE_PATH = Path(__file__).parents[1] / "owui" / "pipe.py"

RESULT = ResearchResult(
    article="# Discovery\n\nThe stone was catalogued in 1783 [1].",
    outline="# Discovery",
    citations=[
        Citation(index=1, url="https://example.org/a", title="Parish survey", snippet="s"),
        Citation(index=2, url="https://example.org/b", title="", snippet="s"),
    ],
    duration_seconds=125.0,
)

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
    spec = importlib.util.spec_from_file_location("owui_kvasir", PIPE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service(monkeypatch, tmp_path):
    """A real kvasir on a real port, with both engines faked."""
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

    def report(settings, store, session_id, stream):
        stream.publish("article", "writing the report")
        calls.append(("report", session_id))
        return Report(report="# Report\n\nFindings.", citations=AGENT_TURN.citations)

    monkeypatch.setattr(main.conversation, "create", create)
    monkeypatch.setattr(main.conversation, "step", step)
    monkeypatch.setattr(main.conversation, "report", report)
    return calls


@pytest.fixture
def pipe(service):
    instance = load_pipe().Pipe()
    instance.valves.KVASIR_URL = service
    return instance


class Events:
    """Collects what the Pipe emitted, split by event type."""

    def __init__(self):
        self.all = []

    async def __call__(self, event):
        self.all.append(event)

    def of(self, kind):
        return [event["data"] for event in self.all if event["type"] == kind]

    @property
    def statuses(self):
        return [data["description"] for data in self.of("status")]


async def run(pipe, body, events=None, metadata=None, call=None, user=None):
    chunks = []
    async for chunk in pipe.pipe(body, events, call, metadata, user):
        chunks.append(chunk)
    return "".join(chunks)


def storm(text):
    return {"model": "kvasir.storm", "messages": [{"role": "user", "content": text}]}


def costorm(text):
    return {"model": "kvasir.co-storm", "messages": [{"role": "user", "content": text}]}


METADATA = {"chat_id": "chat-1"}


def test_it_exposes_both_models():
    assert load_pipe().Pipe().pipes() == [
        {"id": "storm", "name": "STORM"},
        {"id": "co-storm", "name": "Co-STORM"},
    ]


# -- STORM -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_returns_the_article_after_a_reasoning_block(pipe):
    events = Events()

    answer = await run(pipe, storm("The Kvasir stone"), events)

    assert answer.count("<think>") == 1
    assert answer.count("</think>") == 1
    reasoning, body = answer.split("</think>", 1)
    assert "- asking questions" in reasoning
    assert "**research**" in reasoning and "**polish**" in reasoning
    assert body.lstrip().startswith("# Discovery")

    assert "research: asking questions" in events.statuses
    assert events.of("status")[-1] == {
        "description": events.statuses[-1],
        "done": True,
    }
    assert events.statuses[-1].startswith("Finished in")


@pytest.mark.asyncio
async def test_every_citation_becomes_a_source_event(pipe):
    events = Events()

    answer = await run(pipe, storm("The Kvasir stone"), events)

    sources = events.of("source")
    assert [data["source"]["name"] for data in sources] == [
        "[1] Parish survey",
        # A source with no title falls back to its URL rather than rendering an empty name.
        "[2] https://example.org/b",
    ]
    assert sources[0]["metadata"][0]["url"] == "https://example.org/a"
    # The sources panel replaces the markdown list the pipe used to append.
    assert "## References" not in answer


@pytest.mark.asyncio
async def test_it_reports_what_the_run_spent(pipe):
    answer = await run(pipe, storm("The Kvasir stone"))

    footer = answer.split("</think>", 1)[1]
    assert "<summary>Run " in footer
    assert "| stage | duration |" in footer
    # Nothing priced this run, and zero is not the same as free.
    assert "cost not reported" in footer


@pytest.mark.asyncio
async def test_the_usage_footer_can_be_turned_off(pipe):
    pipe.valves.SHOW_USAGE = False

    assert "<summary>Run " not in await run(pipe, storm("x"))


@pytest.mark.asyncio
async def test_the_first_message_names_the_chat(pipe):
    events = Events()

    await run(pipe, storm("The Kvasir stone"), events)

    assert events.of("chat:title") == [{"title": "The Kvasir stone"}]
    assert events.of("chat:tags") == [{"tags": ["kvasir", "storm"]}]


@pytest.mark.asyncio
async def test_a_follow_up_asks_before_starting_another_run(pipe):
    asked = []

    async def call(event):
        asked.append(event)
        return False

    body = storm("another topic")
    body["messages"].insert(0, {"role": "assistant", "content": "an article"})
    body["messages"].insert(0, {"role": "user", "content": "the first topic"})

    answer = await run(pipe, body, call=call)

    assert asked[0]["type"] == "confirmation"
    assert answer.startswith("Cancelled")
    assert "<think>" not in answer


@pytest.mark.asyncio
async def test_a_confirmed_follow_up_researches_again(pipe):
    async def call(event):
        return True

    body = storm("another topic")
    body["messages"].insert(0, {"role": "assistant", "content": "an article"})

    assert "# Discovery" in await run(pipe, body, call=call)
    # A renamed chat is the user's, so a follow-up leaves the title alone.


@pytest.mark.asyncio
async def test_a_failed_run_closes_the_reasoning_block(pipe, monkeypatch):
    def failing_run(settings, request, stream):
        raise RuntimeError("searxng returned nothing")

    monkeypatch.setattr(main, "run_research", failing_run)
    events = Events()

    answer = await run(pipe, storm("x"), events)

    assert "searxng returned nothing" in answer
    assert answer.count("<think>") == answer.count("</think>") == 1
    assert events.of("notification")[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_it_reports_saturation_rather_than_hanging(pipe):
    assert main.app.state.run_slots.acquire(blocking=False)
    try:
        answer = await run(pipe, storm("x"))
    finally:
        main.app.state.run_slots.release()

    assert "429" in answer and "busy" in answer


@pytest.mark.asyncio
async def test_an_unreachable_service_is_reported_not_raised(pipe):
    pipe.valves.KVASIR_URL = "http://127.0.0.1:1"

    assert "Could not reach kvasir" in await run(pipe, storm("x"))


@pytest.mark.asyncio
async def test_it_asks_for_a_topic_when_there_is_none(pipe):
    asked = []

    async def call(event):
        asked.append(event)
        return "The Kvasir stone"

    answer = await run(pipe, {"model": "kvasir.storm", "messages": []}, call=call)

    assert asked[0]["type"] == "input"
    assert "# Discovery" in answer


@pytest.mark.asyncio
async def test_without_a_topic_and_without_a_dialog_it_says_so(pipe):
    assert await run(pipe, {"model": "kvasir.storm", "messages": []}) == "Send a topic to research."


@pytest.mark.asyncio
async def test_only_the_last_user_message_is_researched(pipe, monkeypatch):
    sent = {}

    def capture(settings, request, stream):
        sent["topic"] = request.topic
        return RESULT

    monkeypatch.setattr(main, "run_research", capture)
    await run(
        pipe,
        {
            "model": "kvasir.storm",
            "messages": [
                {"role": "user", "content": "an earlier topic"},
                {"role": "assistant", "content": "an article"},
                {"role": "user", "content": "the real topic"},
            ],
        },
        call=None,
    )

    assert sent["topic"] == "the real topic"


@pytest.mark.asyncio
async def test_unset_valves_leave_the_defaults_to_the_service(pipe, monkeypatch):
    sent = {}

    def capture(settings, request, stream):
        sent["request"] = request
        return RESULT

    monkeypatch.setattr(main, "run_research", capture)
    await run(pipe, storm("x"))

    request = sent["request"]
    assert request.search_top_k is None
    assert request.max_conv_turn is None
    assert request.model_fast is None


@pytest.mark.asyncio
async def test_a_user_valve_overrides_the_admin_one(pipe, monkeypatch):
    sent = {}

    def capture(settings, request, stream):
        sent["request"] = request
        return RESULT

    monkeypatch.setattr(main, "run_research", capture)
    pipe.valves.SEARCH_TOP_K = 3
    user = {"valves": {"SEARCH_TOP_K": 9, "POLISH": "off"}}

    await run(pipe, storm("x"), user=user)

    assert sent["request"].search_top_k == 9
    assert sent["request"].do_polish_article is False


# -- Co-STORM ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_message_warm_starts_and_takes_a_turn(pipe, engine):
    events = Events()

    answer = await run(pipe, costorm("The Kvasir stone"), events, METADATA)

    assert engine[0] == ("create", "The Kvasir stone")
    assert engine[1] == ("step", "")
    assert "Round table on **The Kvasir stone**" in answer
    assert "Petrologist, Local historian" in answer
    assert "four hundred kilometres north" in answer
    assert "_The mind map was reorganised._" in answer
    # Both calls share one reasoning block rather than opening a second.
    assert answer.count("<think>") == 1
    assert "- gathering background" in answer
    assert events.of("chat:tags") == [{"tags": ["kvasir", "co-storm"]}]
    assert [data["source"]["name"] for data in events.of("source")] == ["[1] Provenance"]


@pytest.mark.asyncio
async def test_warm_start_can_stop_without_taking_a_turn(pipe, engine):
    pipe.valves.ADVANCE_AFTER_WARM_START = False

    answer = await run(pipe, costorm("The Kvasir stone"), None, METADATA)

    assert [name for name, _ in engine] == ["create"]
    assert "Say `next` to advance" in answer


@pytest.mark.asyncio
async def test_the_session_is_keyed_by_chat_id_and_reused(pipe, engine):
    await run(pipe, costorm("The Kvasir stone"), None, METADATA)
    engine.clear()

    await run(pipe, costorm("next"), None, METADATA)

    # No second create: the existing session was found on disk.
    assert [name for name, _ in engine] == ["step"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["next", "NEXT", " next ", "/next"])
async def test_advance_takes_an_agent_turn_without_speaking(pipe, engine, command):
    await run(pipe, costorm("The Kvasir stone"), None, METADATA)
    engine.clear()

    answer = await run(pipe, costorm(command), None, METADATA)

    assert engine == [("step", "")]
    assert "Petrologist" in answer


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["report", "/report", "Report"])
async def test_report_returns_the_markdown_with_sources(pipe, engine, command):
    await run(pipe, costorm("The Kvasir stone"), None, METADATA)
    engine.clear()
    events = Events()

    answer = await run(pipe, costorm(command), events, METADATA)

    assert [name for name, _ in engine] == ["report"]
    reasoning, body = answer.split("</think>", 1)
    assert "- writing the report" in reasoning
    assert body.lstrip().startswith("# Report")
    assert [data["source"]["name"] for data in events.of("source")] == ["[1] Provenance"]
    # The report is a run like any other now, so it carries a run id and a usage footer.
    assert "<summary>Run " in body


@pytest.mark.asyncio
async def test_speaking_records_the_turn_then_asks_for_a_response(pipe, engine):
    await run(pipe, costorm("The Kvasir stone"), None, METADATA)
    engine.clear()

    answer = await run(pipe, costorm("How was it dated?"), None, METADATA)

    # A user turn is recorded rather than answered, so a second step produces the reply.
    assert engine == [("step", "How was it dated?"), ("step", "")]
    reasoning, body = answer.split("</think>", 1)
    assert "recorded as Original Question" in reasoning
    assert "four hundred kilometres north" in body


@pytest.mark.asyncio
async def test_a_failing_call_is_reported_not_raised(pipe, engine, monkeypatch):
    def failing(settings, store, request, stream):
        raise RuntimeError("searxng returned nothing")

    monkeypatch.setattr(main.conversation, "create", failing)

    assert "searxng returned nothing" in await run(pipe, costorm("x"), None, METADATA)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["next", "report"])
async def test_a_command_before_the_round_table_exists_asks_for_a_topic(pipe, engine, command):
    events = Events()

    answer = await run(pipe, costorm(command), events, METADATA)

    assert engine == []
    assert answer.startswith("No round table in this chat yet")
    # Nothing ran, so nothing claims to have finished.
    assert events.of("notification") == []


@pytest.mark.asyncio
async def test_a_missing_chat_id_is_reported(pipe, engine):
    answer = await run(pipe, costorm("The Kvasir stone"), None, {})

    assert "did not supply a chat id" in answer


@pytest.mark.asyncio
async def test_an_empty_message_asks_for_a_topic(pipe, engine):
    body = {"model": "kvasir.co-storm", "messages": []}

    assert await run(pipe, body, None, METADATA) == "Send a topic."
