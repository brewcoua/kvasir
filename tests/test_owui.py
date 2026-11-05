"""Drives the Open WebUI Pipe against a real running service over a real socket.

The Pipe is pasted into Open WebUI by hand and cannot be imported as a package, so it is loaded
from its path. Running it against uvicorn rather than a mock is the point: SSE parsing and chunk
boundaries are exactly what a mocked transport would fail to exercise. Only the two engines are
faked; routing, the run registry and SSE framing are all real.
"""

import importlib.util
import socket
import threading
from pathlib import Path

import pytest
import uvicorn

from fakes import ENVIRONMENT, STORED, store_settings, stub_method
from kvasir.api.app import app
from kvasir.schemas.results import (
    ArticleResult,
    Citation,
    RemovedSentence,
    ResearchResult,
)

PIPE_PATH = Path(__file__).parents[1] / "owui" / "pipe.py"

RESULT = ArticleResult(
    article="# Discovery\n\nThe stone was catalogued in 1783 [1].",
    outline="# Discovery",
    citations=[
        Citation(index=1, url="https://example.org/a", title="Parish survey", snippet="s"),
        Citation(index=2, url="https://example.org/b", title="", snippet="s"),
    ],
    duration_seconds=125.0,
)

RESEARCH = ResearchResult(
    report="# The Kvasir stone\n\nIt was catalogued in 1783 [1].",
    assumptions=["Taken to mean the runestone, not the mead."],
    citations=[Citation(index=1, url="https://example.org/a", title="Parish survey", snippet="s")],
    claims=[],
    removed=[
        RemovedSentence(section="Origin", sentence="It is Roman [4].", reason="no quote says so")
    ],
    duration_seconds=300.0,
)


def load_pipe():
    spec = importlib.util.spec_from_file_location("owui_kvasir", PIPE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service(monkeypatch, tmp_path):
    """A real kvasir on a real port, with both engines faked."""
    # Startup creates the database, and the default /data is not writable here.
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    store_settings(tmp_path / "data", STORED)
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    def fake_run(settings, input, options, stream):
        stream.publish("research", "asking questions")
        stream.publish("polish", "polishing the article")
        return RESULT

    stub_method(monkeypatch, "storm", fake_run)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
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
    instance = load_pipe().Pipe()
    instance.valves.KVASIR_URL = service
    instance.valves.KVASIR_TOKEN = "token"
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


async def run(pipe, body, events=None, call=None, user=None, task=None):
    chunks = []
    async for chunk in pipe.pipe(body, events, call, user, task):
        chunks.append(chunk)
    return "".join(chunks)


def storm(text):
    return {"model": "kvasir.storm", "messages": [{"role": "user", "content": text}]}


# Open WebUI's own title prompt, trimmed. The shape is what matters: a long instruction addressed
# to the model, with the real conversation buried in it, arriving as an ordinary user message.
TITLE_PROMPT = """### Task:
Generate a concise title summarizing the chat history.

### Output:
JSON format: { "title": "your concise title here" }

### Chat History:
<chat_history>
USER: The Kvasir stone
</chat_history>"""


def test_it_exposes_the_two_models():
    assert load_pipe().Pipe().pipes() == [
        {"id": "storm", "name": "STORM"},
        {"id": "research", "name": "Research"},
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
async def test_it_leaves_the_title_and_tags_to_open_webui(pipe):
    """Open WebUI generates both by asking this pipe, and its answers overwrite ours anyway."""
    events = Events()

    await run(pipe, storm("The Kvasir stone"), events)

    assert events.of("chat:title") == []
    assert events.of("chat:tags") == []


@pytest.mark.asyncio
async def test_a_task_goes_to_the_fast_model_and_takes_no_run(pipe, monkeypatch):
    """Open WebUI asks the chat's own model for the title, the tags and the follow-up prompts.

    Answered as research those are three more runs, and with one run slot they are refused by the
    run they describe.
    """
    researched = []
    stub_method(monkeypatch, "storm", lambda *args: researched.append(args) or RESULT)
    monkeypatch.setattr(
        "kvasir.api.tasks.build_task_model",
        lambda settings: lambda messages: ['{"title": "Kvasir Stone"}'],
    )
    body = storm(TITLE_PROMPT)

    answer = await run(pipe, body, task="title_generation")

    assert answer == '{"title": "Kvasir Stone"}'
    assert researched == []


@pytest.mark.asyncio
async def test_a_failed_task_is_silent(pipe, monkeypatch):
    def failing(settings):
        raise RuntimeError("the gateway refused")

    monkeypatch.setattr("kvasir.api.tasks.build_task_model", failing)

    # An unnamed chat is better than an error bubble in the conversation.
    assert await run(pipe, storm(TITLE_PROMPT), task="title_generation") == ""


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


@pytest.mark.asyncio
async def test_a_failed_run_closes_the_reasoning_block(pipe, monkeypatch):
    def failing_run(settings, input, options, stream):
        raise RuntimeError("searxng returned nothing")

    stub_method(monkeypatch, "storm", failing_run)
    events = Events()

    answer = await run(pipe, storm("x"), events)

    assert "searxng returned nothing" in answer
    assert answer.count("<think>") == answer.count("</think>") == 1
    assert events.of("notification")[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_it_reports_saturation_rather_than_hanging(pipe):
    assert app.state.run_slots.acquire()
    try:
        answer = await run(pipe, storm("x"))
    finally:
        app.state.run_slots.release()

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

    def capture(settings, input, options, stream):
        sent["topic"] = input.question
        return RESULT

    stub_method(monkeypatch, "storm", capture)
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

    def capture(settings, input, options, stream):
        sent["options"] = options
        return RESULT

    stub_method(monkeypatch, "storm", capture)
    await run(pipe, storm("x"))

    options = sent["options"]
    assert options.search_top_k is None
    assert options.max_conv_turn is None
    assert options.model_fast is None


@pytest.mark.asyncio
async def test_a_user_valve_overrides_the_admin_one(pipe, monkeypatch):
    sent = {}

    def capture(settings, input, options, stream):
        sent["options"] = options
        return RESULT

    stub_method(monkeypatch, "storm", capture)
    pipe.valves.SEARCH_TOP_K = 3
    user = {"valves": {"SEARCH_TOP_K": 9, "POLISH": "off"}}

    await run(pipe, storm("x"), user=user)

    assert sent["options"].search_top_k == 9
    assert sent["options"].do_polish_article is False


# -- Research ----------------------------------------------------------------------------------


def research(*messages):
    roles = ["user", "assistant"]
    return {
        "model": "kvasir.research",
        "messages": [
            {"role": roles[number % 2], "content": text} for number, text in enumerate(messages)
        ],
    }


@pytest.fixture
def researcher(service, monkeypatch):
    """Fakes clarify and the research run, recording what the pipe asked of each."""
    calls = {"questions": ["Which stone do you mean?", "From what period?"]}

    def clarify(settings, question):
        calls["clarified"] = question
        return calls["questions"]

    def run_research(settings, input, options, stream):
        stream.publish("research", "searching")
        calls["input"] = input
        calls["options"] = options
        return RESEARCH

    monkeypatch.setattr("kvasir.api.tasks.clarify", clarify)
    stub_method(monkeypatch, "ledger", run_research)
    return calls


@pytest.mark.asyncio
async def test_a_new_question_gets_clarifying_questions_and_no_run(pipe, researcher):
    events = Events()
    answer = await run(pipe, research("The Kvasir stone"), events)

    assert researcher["clarified"] == "The Kvasir stone"
    assert "input" not in researcher
    assert answer.startswith("Before researching this, a few questions:")
    assert "1. Which stone do you mean?\n2. From what period?" in answer
    assert "<think>" not in answer
    assert events.of("notification") == []


@pytest.mark.asyncio
async def test_the_reply_answers_the_questions_and_starts_the_run(pipe, researcher):
    asked = await run(pipe, research("The Kvasir stone"))
    events = Events()

    answer = await run(
        pipe, research("The Kvasir stone", asked, "The runestone, Viking age"), events
    )

    asked_for = researcher["input"]
    assert asked_for.question == "The Kvasir stone"
    assert [(c.question, c.answer) for c in asked_for.clarifications] == [
        ("Which stone do you mean?\nFrom what period?", "The runestone, Viking age")
    ]
    assert answer.index("</think>") < answer.index("# The Kvasir stone")
    assert "<summary>Assumed (1)</summary>" in answer
    assert "It is Roman [4]. _no quote says so_" in answer
    assert [s["source"]["name"] for s in events.of("source")] == ["[1] Parish survey"]


@pytest.mark.asyncio
async def test_go_researches_without_answers(pipe, researcher):
    asked = await run(pipe, research("The Kvasir stone"))

    await run(pipe, research("The Kvasir stone", asked, "Go"))

    assert researcher["input"].question == "The Kvasir stone"
    assert researcher["input"].clarifications == []


@pytest.mark.asyncio
async def test_a_clear_question_researches_right_away(pipe, researcher):
    researcher["questions"] = []

    answer = await run(pipe, research("The Kvasir stone"))

    assert researcher["input"].question == "The Kvasir stone"
    assert "# The Kvasir stone" in answer


@pytest.mark.asyncio
async def test_clarifying_can_be_turned_off(pipe, researcher):
    pipe.valves.CLARIFY = False

    await run(pipe, research("The Kvasir stone"))

    assert "clarified" not in researcher
    assert researcher["input"].clarifications == []


@pytest.mark.asyncio
async def test_a_question_after_a_report_is_a_new_question(pipe, researcher):
    await run(pipe, research("The Kvasir stone", "# A report", "And the Rök stone?"))

    assert researcher["clarified"] == "And the Rök stone?"


@pytest.mark.asyncio
async def test_research_budgets_reach_the_service_only_when_set(pipe, researcher):
    researcher["questions"] = []
    pipe.valves.MAX_SEARCHES = 2
    pipe.valves.FOLLOW_UPS = "off"

    await run(pipe, research("The Kvasir stone"))

    options = researcher["options"]
    assert options.max_searches == 2
    assert options.follow_ups is False
    assert options.results_per_query is None
