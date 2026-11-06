"""Drives the Open WebUI Pipe against the service over a real socket.

The Pipe is pasted into Open WebUI by hand and cannot be imported as a package, so it is loaded
from its path. Running it against the service rather than a mock is the point: SSE parsing and
chunk boundaries are exactly what a mocked transport would fail to exercise. Only the provider and
search are faked, by the harness.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest
from harness import TOKEN, Fakes, Service
from test_contract_runs import _start, body

PIPE_PATH = Path(__file__).parents[2] / "owui" / "pipe.py"


def load_pipe() -> Any:
    spec = importlib.util.spec_from_file_location("owui_kvasir", PIPE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sent() -> list[dict[str, Any]]:
    """The run requests the pipe made, in order."""
    return []


@pytest.fixture
def pipe(service: Service, sent: list[dict[str, Any]]) -> Any:
    instance = load_pipe().Pipe()
    instance.valves.KVASIR_URL = service.url
    instance.valves.KVASIR_TOKEN = TOKEN
    call = instance._call

    def recording(http: Any, method: str, path: str, payload: Any, *rest: Any) -> Any:
        sent.append(payload)
        return call(http, method, path, payload, *rest)

    instance._call = recording
    return instance


class Events:
    """Collects what the Pipe emitted, split by event type."""

    def __init__(self) -> None:
        self.all: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.all.append(event)

    def of(self, kind: str) -> list[Any]:
        return [event["data"] for event in self.all if event["type"] == kind]

    @property
    def statuses(self) -> list[str]:
        return [data["description"] for data in self.of("status")]


async def run(
    pipe: Any,
    body: dict[str, Any],
    events: Any = None,
    call: Any = None,
    user: Any = None,
    task: Any = None,
) -> str:
    chunks = []
    async for chunk in pipe.pipe(body, events, call, user, task):
        chunks.append(chunk)
    return "".join(chunks)


def storm(text: str) -> dict[str, Any]:
    return {"model": "kvasir.storm", "messages": [{"role": "user", "content": text}]}


def runs(client: httpx.Client) -> list[str]:
    return [run["id"] for run in client.get("/v1/runs").json()]


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


def test_it_exposes_the_two_models() -> None:
    assert load_pipe().Pipe().pipes() == [
        {"id": "storm", "name": "STORM"},
        {"id": "research", "name": "Research"},
    ]


# -- STORM -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_returns_the_article_after_a_reasoning_block(pipe: Any) -> None:
    events = Events()

    answer = await run(pipe, storm("The Kvasir stone"), events)

    assert answer.count("<think>") == 1
    assert answer.count("</think>") == 1
    reasoning, article = answer.split("</think>", 1)
    assert "**research**" in reasoning and "**polish**" in reasoning
    assert "# Origin" in article

    assert any(status.startswith("research: ") for status in events.statuses)
    assert events.of("status")[-1] == {"description": events.statuses[-1], "done": True}
    assert events.statuses[-1].startswith("Finished in")


@pytest.mark.asyncio
async def test_every_citation_becomes_a_source_event(pipe: Any, fakes: Fakes) -> None:
    events = Events()

    answer = await run(pipe, storm("The Kvasir stone"), events)

    sources = events.of("source")
    assert [data["source"]["name"] for data in sources] == ["[1] The Kvasir stone"]
    assert sources[0]["metadata"][0]["url"] == f"{fakes.url}/web/stone.html"
    # The sources panel replaces the markdown list the pipe used to append.
    assert "## References" not in answer


@pytest.mark.asyncio
async def test_a_source_without_a_title_is_named_by_its_url() -> None:
    events = Events()
    chat = load_pipe()._Chat(events)

    await chat.sources([{"index": 2, "url": "https://example.org/b", "title": "", "snippet": "s"}])

    assert [data["source"]["name"] for data in events.of("source")] == ["[2] https://example.org/b"]


@pytest.mark.asyncio
async def test_it_reports_what_the_run_spent(pipe: Any) -> None:
    answer = await run(pipe, storm("The Kvasir stone"))

    footer = answer.split("</think>", 1)[1]
    assert "<summary>Run " in footer
    assert "| stage | duration |" in footer


@pytest.mark.asyncio
async def test_the_usage_footer_can_be_turned_off(pipe: Any) -> None:
    pipe.valves.SHOW_USAGE = False

    assert "<summary>Run " not in await run(pipe, storm("x"))


@pytest.mark.asyncio
async def test_it_leaves_the_title_and_tags_to_open_webui(pipe: Any) -> None:
    """Open WebUI generates both by asking this pipe, and its answers overwrite ours anyway."""
    events = Events()

    await run(pipe, storm("The Kvasir stone"), events)

    assert events.of("chat:title") == []
    assert events.of("chat:tags") == []


@pytest.mark.asyncio
async def test_a_task_goes_to_the_fast_model_and_takes_no_run(
    pipe: Any, client: httpx.Client, fakes: Fakes
) -> None:
    """Open WebUI asks the chat's own model for the title, the tags and the follow-up prompts.

    Answered as research those are three more runs, and with one run slot they are refused by the
    run they describe.
    """
    before = runs(client)

    answer = await run(pipe, storm(TITLE_PROMPT), task="title_generation")

    assert answer == "A title"
    assert runs(client) == before
    chats = [request for path, request in fakes.requests if path.endswith("/chat/completions")]
    assert chats[-1]["model"] == "fast"


@pytest.mark.asyncio
async def test_a_failed_task_is_silent(pipe: Any, fakes: Fakes) -> None:
    fakes.chat_status = 400
    try:
        # An unnamed chat is better than an error bubble in the conversation.
        assert await run(pipe, storm(TITLE_PROMPT), task="title_generation") == ""
    finally:
        fakes.chat_status = None


@pytest.mark.asyncio
async def test_a_follow_up_asks_before_starting_another_run(
    pipe: Any, sent: list[dict[str, Any]]
) -> None:
    asked = []

    async def call(event: dict[str, Any]) -> bool:
        asked.append(event)
        return False

    body = storm("another topic")
    body["messages"].insert(0, {"role": "assistant", "content": "an article"})
    body["messages"].insert(0, {"role": "user", "content": "the first topic"})

    answer = await run(pipe, body, call=call)

    assert asked[0]["type"] == "confirmation"
    assert answer.startswith("Cancelled")
    assert "<think>" not in answer
    assert sent == []


@pytest.mark.asyncio
async def test_a_confirmed_follow_up_researches_again(pipe: Any) -> None:
    async def call(event: dict[str, Any]) -> bool:
        return True

    body = storm("another topic")
    body["messages"].insert(0, {"role": "assistant", "content": "an article"})

    assert "# Origin" in await run(pipe, body, call=call)


@pytest.mark.asyncio
async def test_a_failed_run_closes_the_reasoning_block(pipe: Any, fakes: Fakes) -> None:
    events = Events()
    fakes.chat_status = 400
    try:
        answer = await run(pipe, storm("x"), events)
    finally:
        fakes.chat_status = None

    assert answer.count("<think>") == answer.count("</think>") == 1
    assert answer.split("</think>", 1)[1].strip()
    assert events.of("notification")[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_it_reports_saturation_rather_than_hanging(
    pipe: Any, service: Service, fakes: Fakes
) -> None:
    with service.client() as other:
        with fakes.holding_chat():
            thread, _ = _start(other, body("ledger"))
            assert fakes.held.wait(30)

            answer = await run(pipe, storm("x"))
        thread.join(60)

    assert "429" in answer and "busy" in answer


@pytest.mark.asyncio
async def test_an_unreachable_service_is_reported_not_raised(pipe: Any) -> None:
    pipe.valves.KVASIR_URL = "http://127.0.0.1:1"

    assert "Could not reach kvasir" in await run(pipe, storm("x"))


@pytest.mark.asyncio
async def test_it_asks_for_a_topic_when_there_is_none(pipe: Any) -> None:
    asked = []

    async def call(event: dict[str, Any]) -> str:
        asked.append(event)
        return "The Kvasir stone"

    answer = await run(pipe, {"model": "kvasir.storm", "messages": []}, call=call)

    assert asked[0]["type"] == "input"
    assert "# Origin" in answer


@pytest.mark.asyncio
async def test_without_a_topic_and_without_a_dialog_it_says_so(pipe: Any) -> None:
    assert await run(pipe, {"model": "kvasir.storm", "messages": []}) == "Send a topic to research."


@pytest.mark.asyncio
async def test_only_the_last_user_message_is_researched(
    pipe: Any, sent: list[dict[str, Any]]
) -> None:
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

    assert sent[0]["input"] == {"question": "the real topic"}


@pytest.mark.asyncio
async def test_unset_valves_leave_the_defaults_to_the_service(
    pipe: Any, sent: list[dict[str, Any]]
) -> None:
    await run(pipe, storm("x"))

    assert sent[0]["options"] == {"do_polish_article": True}


@pytest.mark.asyncio
async def test_a_user_valve_overrides_the_admin_one(pipe: Any, sent: list[dict[str, Any]]) -> None:
    pipe.valves.SEARCH_TOP_K = 3
    user = {"valves": {"SEARCH_TOP_K": 9, "POLISH": "off"}}

    await run(pipe, storm("x"), user=user)

    assert sent[0]["options"] == {"do_polish_article": False, "search_top_k": 9}


# -- Research ----------------------------------------------------------------------------------


def research(*messages: str) -> dict[str, Any]:
    roles = ["user", "assistant"]
    return {
        "model": "kvasir.research",
        "messages": [
            {"role": roles[number % 2], "content": text} for number, text in enumerate(messages)
        ],
    }


# The harness's clarify answers no questions for a question that says it is clear.
CLEAR = "A clear question about the Kvasir stone"


@pytest.fixture
def researcher(pipe: Any) -> Any:
    """Kept small, so a run through the fakes ends quickly."""
    pipe.valves.MAX_SEARCHES = 1
    pipe.valves.RESULTS_PER_QUERY = 1
    pipe.valves.FOLLOW_UPS = "off"
    return pipe


@pytest.mark.asyncio
async def test_a_new_question_gets_clarifying_questions_and_no_run(
    researcher: Any, sent: list[dict[str, Any]]
) -> None:
    events = Events()
    answer = await run(researcher, research("The Kvasir stone"), events)

    assert sent == []
    assert answer.startswith("Before researching this, a few questions:")
    assert "1. Which stone do you mean?" in answer
    assert "<think>" not in answer
    assert events.of("notification") == []


@pytest.mark.asyncio
async def test_the_reply_answers_the_questions_and_starts_the_run(
    researcher: Any, sent: list[dict[str, Any]], fakes: Fakes
) -> None:
    asked = await run(researcher, research("The Kvasir stone"))
    events = Events()

    answer = await run(
        researcher, research("The Kvasir stone", asked, "The runestone, Viking age"), events
    )

    assert sent[0]["input"] == {
        "question": "The Kvasir stone",
        "clarifications": [
            {"question": "Which stone do you mean?", "answer": "The runestone, Viking age"}
        ],
    }
    assert answer.index("</think>") < answer.index("1783")
    assert "<summary>Assumed (1)</summary>" in answer
    assert [s["source"]["name"] for s in events.of("source")] == ["[1] The Kvasir stone"]


@pytest.mark.asyncio
async def test_go_researches_without_answers(researcher: Any, sent: list[dict[str, Any]]) -> None:
    asked = await run(researcher, research("The Kvasir stone"))

    await run(researcher, research("The Kvasir stone", asked, "Go"))

    assert sent[0]["input"] == {"question": "The Kvasir stone", "clarifications": []}


@pytest.mark.asyncio
async def test_a_clear_question_researches_right_away(
    researcher: Any, sent: list[dict[str, Any]]
) -> None:
    answer = await run(researcher, research(CLEAR))

    assert sent[0]["input"]["question"] == CLEAR
    assert "1783" in answer.split("</think>", 1)[1]


@pytest.mark.asyncio
async def test_clarifying_can_be_turned_off(researcher: Any, sent: list[dict[str, Any]]) -> None:
    researcher.valves.CLARIFY = False
    clarified = []

    async def clarify(http: Any, question: str) -> list[str]:
        clarified.append(question)
        return []

    researcher._clarify = clarify

    await run(researcher, research("The Kvasir stone"))

    assert clarified == []
    assert sent[0]["input"] == {"question": "The Kvasir stone", "clarifications": []}


@pytest.mark.asyncio
async def test_a_question_after_a_report_is_a_new_question(
    researcher: Any, sent: list[dict[str, Any]]
) -> None:
    await run(researcher, research("The Kvasir stone", "# A report", f"And {CLEAR}?"))

    assert sent[0]["input"]["question"] == f"And {CLEAR}?"


@pytest.mark.asyncio
async def test_research_budgets_reach_the_service_only_when_set(
    pipe: Any, sent: list[dict[str, Any]]
) -> None:
    pipe.valves.MAX_SEARCHES = 1
    pipe.valves.FOLLOW_UPS = "off"

    await run(pipe, research(CLEAR))

    assert sent[0]["options"] == {"max_searches": 1, "follow_ups": False}
