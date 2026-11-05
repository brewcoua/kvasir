"""Route behaviour with the STORM run faked. No network, no model calls."""

import asyncio
import json
import threading
from typing import get_args

import httpx
import pytest
from fastapi.testclient import TestClient

from fakes import AUTH, ENVIRONMENT, STORED, article_run, store_settings, stub_method
from kvasir import conversation
from kvasir.api import streaming
from kvasir.api.app import app
from kvasir.methods import METHODS
from kvasir.runs.registry import RunRegistry
from kvasir.schemas.requests import RunRequest
from kvasir.schemas.results import (
    ArticleResult,
    Citation,
    Report,
    ResearchResult,
    SessionInfo,
    Turn,
)

RESULT = ArticleResult(
    article="# Discovery\n\nText [1].",
    outline="# Discovery",
    citations=[Citation(index=1, url="https://example.org/a", title="A", snippet="s")],
    duration_seconds=1.0,
)


@pytest.fixture
def environment(monkeypatch, tmp_path):
    """The data directory, with the settings every route needs stored in it."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    # Startup creates the sessions directory, and the default /data is not writable here.
    data = tmp_path / "data"
    monkeypatch.setenv("KVASIR_DATA_DIR", str(data))
    store_settings(data, STORED)
    return data


@pytest.fixture
def client(environment):
    with TestClient(app, headers=AUTH) as client:
        yield client


def parse(text):
    """Split an SSE body into (event, payload) pairs."""
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        events.append(
            (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        )
    return events


def test_healthz_checks_no_dependency(client):
    # A gateway outage must not restart the pod, so this stays green regardless.
    assert client.get("/healthz").json() == {"status": "ok"}


def test_index_serves_the_built_page(client, monkeypatch, tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<!doctype html>")
    monkeypatch.setattr("kvasir.api.app.INDEX", page)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_index_says_when_the_page_is_not_built(client, monkeypatch, tmp_path):
    monkeypatch.setattr("kvasir.api.app.INDEX", tmp_path / "index.html")
    response = client.get("/")
    assert response.status_code == 503
    assert "pnpm build" in response.text


def test_methods_report_the_configured_defaults(environment, monkeypatch):
    store_settings(environment, {"research.max_searches": 9, "research.follow_ups": False})

    with TestClient(app, headers=AUTH) as client:
        methods = {method["name"]: method for method in client.get("/v1/methods").json()}

    assert set(methods) == set(METHODS)
    ledger = methods["ledger"]["options"]["properties"]
    storm = methods["storm"]["options"]["properties"]
    assert ledger["max_searches"]["default"] == 9
    assert ledger["follow_ups"]["default"] is False
    # No checker is configured, so grounding is off.
    assert ledger["ground"]["default"] is False
    assert ledger["corroborate"]["default"] is False
    assert ledger["max_age_hours"]["default"] == 48
    assert ledger["recall_past_runs"]["default"] is True
    assert ledger["model_fast"]["default"] == STORED["model.fast"]
    assert storm["do_polish_article"]["default"] is True
    assert storm["search_top_k"]["default"] == 3
    assert methods["ledger"]["clarifies"] is True
    assert methods["storm"]["stages"] == ["research", "outline", "article", "polish"]


def test_every_method_can_be_requested():
    members = get_args(get_args(RunRequest)[0])
    assert {member.model_fields["method"].annotation.__args__[0] for member in members} == set(
        METHODS
    )


def test_an_unknown_option_is_refused(client):
    body = article_run("x", max_searches=3)
    assert client.post("/v1/runs", json=body).status_code == 422


@pytest.mark.parametrize("method", ["ledger", "graph"])
def test_a_negative_max_age_is_refused(client, method):
    body = {"method": method, "input": {"question": "x"}, "options": {"max_age_hours": -1}}
    assert client.post("/v1/runs", json=body).status_code == 422


@pytest.mark.parametrize("option", [{"max_age_hours": 0}, {"recall_past_runs": False}])
def test_storm_refuses_the_cache_options(client, option):
    assert client.post("/v1/runs", json=article_run("x", **option)).status_code == 422


def test_an_unknown_method_is_refused(client):
    body = {"method": "nope", "input": {"question": "x"}}
    assert client.post("/v1/runs", json=body).status_code == 422


def test_startup_fails_loudly_on_missing_configuration():
    with pytest.raises(Exception, match="KVASIR_TOKEN"), TestClient(app, headers=AUTH):
        pass


@pytest.fixture
def unconfigured(monkeypatch, tmp_path):
    """A first start: the environment is set, nothing is stored."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app, headers=AUTH) as client:
        yield client


def test_a_first_start_is_not_ready_and_says_what_to_set(unconfigured):
    response = unconfigured.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "setup": "required",
        "missing": ["model.fast", "model.strong", "model.embedding", "search.url"],
    }


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/runs", article_run("x")),
        ("/v1/clarify", {"question": "x"}),
        ("/v1/tasks", {"messages": [{"role": "user", "content": "x"}]}),
        ("/v1/session", {"session_id": "s", "topic": "x"}),
        ("/v1/session/s/step", {}),
        ("/v1/session/s/report", None),
    ],
)
def test_nothing_runs_before_the_setup(unconfigured, path, body):
    response = unconfigured.post(path, json=body)

    assert response.status_code == 409
    assert response.json()["message"].startswith("set these settings first: model.fast")
    assert unconfigured.get("/v1/runs").json() == []


def test_a_first_start_still_lists_and_describes(unconfigured):
    assert unconfigured.get("/v1/runs").status_code == 200
    assert unconfigured.get("/v1/methods").status_code == 200


def test_a_stored_model_whose_provider_is_gone_needs_setting_again(environment, monkeypatch):
    store_settings(environment, {"model.strong": "gone/strong"})

    with TestClient(app, headers=AUTH) as client:
        response = client.get("/readyz")

    assert response.json() == {"setup": "required", "missing": ["model.strong"]}


@pytest.mark.parametrize(
    ("gateway_ok", "searxng_ok", "expected"),
    [(True, True, 200), (False, True, 503), (True, False, 503), (False, False, 503)],
)
def test_readyz_hard_fails_when_either_dependency_is_down(
    client, monkeypatch, gateway_ok, searxng_ok, expected
):
    async def reachable(url, headers=None):
        return gateway_ok if "gateway" in url else searxng_ok

    monkeypatch.setattr("kvasir.api.app._reachable", reachable)
    response = client.get("/readyz")

    assert response.status_code == expected
    assert response.json() == {"providers": {"gateway": gateway_ok}, "searxng": searxng_ok}


def test_readyz_treats_an_unreachable_dependency_as_not_ready(client, monkeypatch):
    async def refuse(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx.AsyncClient, "get", refuse)

    assert client.get("/readyz").status_code == 503


def test_readyz_probes_each_provider_a_model_uses_once(environment, monkeypatch):
    monkeypatch.setenv("KVASIR_PROVIDER_OTHER_API_BASE", "https://other.example/v1")
    monkeypatch.setenv("KVASIR_PROVIDER_UNUSED_API_BASE", "https://unused.example/v1")
    store_settings(environment, {"model.strong": "other/strong"})
    probed = []

    async def reachable(url, headers=None):
        probed.append(url)
        return "other" not in url

    monkeypatch.setattr("kvasir.api.app._reachable", reachable)
    with TestClient(app, headers=AUTH) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"providers": {"gateway": True, "other": False}, "searxng": True}
    assert sorted(probed) == [
        "http://searxng.example",
        "https://gateway.example/v1/models",
        "https://other.example/v1/models",
    ]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/runs", article_run("x", model_strong="nope/x")),
        ("/v1/session", {"session_id": "chat-1", "topic": "x", "model_fast": "nope/x"}),
    ],
)
def test_a_model_override_naming_an_unknown_provider_is_a_client_error(client, path, body):
    response = client.post(path, json=body)

    assert response.status_code == 400
    assert "unknown provider 'nope'" in response.json()["message"]
    assert client.get("/v1/runs").json() == []


def test_article_streams_progress_then_the_article(client, monkeypatch):
    def fake_run(settings, input, options, stream):
        stream.publish("research", "asking questions")
        stream.publish("article", "writing sections")
        return RESULT

    stub_method(monkeypatch, "storm", fake_run)
    response = client.post("/v1/runs", json=article_run("The Kvasir stone"))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse(response.text)
    assert [name for name, _ in events] == ["run", "progress", "progress", "done"]
    assert events[0][1]["run_id"]
    assert events[1][1]["stage"] == "research"
    assert events[1][1]["detail"] == "asking questions"
    assert events[1][1]["at"] > 0
    assert events[-1][1]["article"] == RESULT.article
    assert events[-1][1]["citations"][0]["index"] == 1


def test_article_reports_a_failure_as_an_error_event(client, monkeypatch):
    def fake_run(settings, input, options, stream):
        raise RuntimeError("gateway refused the connection")

    stub_method(monkeypatch, "storm", fake_run)
    events = parse(client.post("/v1/runs", json=article_run("x")).text)

    assert [name for name, _ in events] == ["run", "error"]
    assert "gateway refused the connection" in events[-1][1]["message"]


def test_article_rejects_an_empty_topic(client):
    assert client.post("/v1/runs", json=article_run("")).status_code == 422


def test_research_streams_progress_then_the_report(client, monkeypatch):
    seen = {}

    def fake_run(settings, input, options, stream):
        seen["input"] = input
        stream.publish("brief", "writing the research brief")
        return ResearchResult(
            report="## Findings\n\nText [1].",
            assumptions=["Europe"],
            citations=RESULT.citations,
            claims=[],
            duration_seconds=1.0,
        )

    stub_method(monkeypatch, "ledger", fake_run)
    body = {
        "method": "ledger",
        "input": {
            "question": "Which battery makers lead?",
            "clarifications": [{"question": "Region?", "answer": "Europe"}],
        },
    }
    events = parse(client.post("/v1/runs", json=body).text)

    assert [name for name, _ in events] == ["run", "progress", "done"]
    assert events[-1][1]["report"].startswith("## Findings")
    assert seen["input"].clarifications[0].answer == "Europe"
    [run] = client.get("/v1/runs").json()
    assert run["kind"] == "ledger"
    # The brief may choose a ranking, so a run that does not name its output plans for one.
    assert run["planned_stages"] == ["brief", "background", "research", "assess", "write", "verify"]


def test_research_rejects_an_empty_question(client):
    body = {"method": "ledger", "input": {"question": ""}}
    assert client.post("/v1/runs", json=body).status_code == 422


def test_clarify_answers_without_a_run_or_a_slot(client, monkeypatch):
    monkeypatch.setattr("kvasir.api.tasks.clarify", lambda settings, question: ["Which region?"])
    # Held for the whole call, as for a task: clarifying must work while a run is in flight.
    assert app.state.run_slots.acquire(blocking=False)

    try:
        response = client.post("/v1/clarify", json={"question": "best makers"})
    finally:
        app.state.run_slots.release()

    assert response.json() == {"questions": ["Which region?"]}
    assert client.get("/v1/runs").json() == []


def test_a_task_is_answered_by_the_fast_model(client, monkeypatch):
    seen = {}

    def fake_model(settings):
        assert settings.model.fast == STORED["model.fast"]

        def call(messages):
            seen["messages"] = messages
            return ['{"title": "Local LLM Research"}']

        return call

    monkeypatch.setattr("kvasir.api.tasks.build_task_model", fake_model)
    messages = [{"role": "user", "content": "### Task: Generate a concise title"}]
    response = client.post("/v1/tasks", json={"messages": messages})

    assert response.status_code == 200
    assert response.json() == {"content": '{"title": "Local LLM Research"}'}
    assert seen["messages"] == messages


def test_a_task_takes_no_run_and_no_slot(client, monkeypatch):
    monkeypatch.setattr(
        "kvasir.api.tasks.build_task_model", lambda settings: lambda messages: ["ok"]
    )
    # Standing in for the run this task is describing: with one slot, a task routed through
    # /v1/runs would be refused by it. Held for the whole call, so a slot taken and released
    # would not go unnoticed either.
    assert app.state.run_slots.acquire(blocking=False)

    try:
        response = client.post("/v1/tasks", json={"messages": [{"role": "user", "content": "x"}]})
    finally:
        app.state.run_slots.release()

    assert response.status_code == 200
    assert client.get("/v1/runs").json() == []


def test_a_task_needs_at_least_one_message(client):
    assert client.post("/v1/tasks", json={"messages": []}).status_code == 422


def test_a_slot_is_released_after_every_run(client, monkeypatch):
    stub_method(monkeypatch, "storm", lambda *args: RESULT)

    for _ in range(3):
        assert client.post("/v1/runs", json=article_run("x")).status_code == 200


def test_saturation_returns_429_rather_than_queueing(client, monkeypatch):
    stub_method(monkeypatch, "storm", lambda *args: RESULT)
    # Standing in for a run already in flight. TestClient buffers a streaming response, so an
    # actual concurrent request cannot be held open here.
    assert app.state.run_slots.acquire(blocking=False)

    try:
        response = client.post("/v1/runs", json=article_run("second"))
    finally:
        app.state.run_slots.release()

    assert response.status_code == 429
    assert "busy" in response.json()["message"]
    # Refusal must not consume a slot, or the service deadlocks after the first refusal.
    assert client.post("/v1/runs", json=article_run("third")).status_code == 200


@pytest.mark.asyncio
async def test_a_client_that_disconnects_keeps_the_slot_until_the_work_ends(environment):
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    finish = threading.Event()

    def work(stream):
        finish.wait(5)
        return RESULT

    record = RunRegistry(10).start("storm", "x")
    events = streaming.events(work, "x", record, slots)
    await anext(events)
    # Waiting for the first progress event when the client goes away, as a disconnect does.
    reading = asyncio.create_task(anext(events))
    await asyncio.sleep(0.1)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading

    assert not slots.acquire(blocking=False)
    finish.set()
    assert await asyncio.to_thread(slots.acquire, True, 5)


@pytest.mark.asyncio
async def test_a_client_that_leaves_before_the_work_starts_frees_the_slot(environment):
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    record = RunRegistry(10).start("storm", "x")

    events = streaming.events(lambda stream: RESULT, "x", record, slots)
    await anext(events)
    await events.aclose()

    assert slots.acquire(blocking=False)


@pytest.fixture
def sessions(client):
    """The store the running app is actually using, under the temporary data directory."""
    return app.state.sessions


TURN = Turn(
    role="Petrologist",
    role_description="studies rock provenance",
    utterance="The source outcrop is northern [1].",
    utterance_type="Support",
    citations=[Citation(index=1, url="https://example.org/a", title="A", snippet="s")],
    mind_map_reorganised=True,
)

INFO = SessionInfo(
    session_id="chat-1",
    topic="The Kvasir stone",
    turn_count=2,
    experts=["Petrologist"],
    updated_at=1.0,
)


def test_creating_a_session_streams_warm_start_progress(client, sessions, monkeypatch):
    def fake_create(settings, store, request, stream):
        stream.publish("warm_start", "gathering background")
        return INFO

    monkeypatch.setattr(conversation, "create", fake_create)
    events = parse(client.post("/v1/session", json={"session_id": "chat-1", "topic": "x"}).text)

    assert [name for name, _ in events] == ["run", "progress", "done"]
    assert events[1][1]["stage"] == "warm_start"
    assert events[-1][1]["experts"] == ["Petrologist"]


def test_creating_a_session_twice_is_refused(client, sessions, monkeypatch):
    monkeypatch.setattr(conversation, "create", lambda *args: INFO)
    sessions.save("chat-1", {"runner_argument": {"topic": "x"}, "conversation_history": []})

    response = client.post("/v1/session", json={"session_id": "chat-1", "topic": "x"})

    assert response.status_code == 409


def test_an_unsafe_session_id_is_rejected_before_touching_the_filesystem(client, sessions):
    response = client.post("/v1/session", json={"session_id": "../escape", "topic": "x"})

    assert response.status_code == 400
    assert "session id must be" in response.json()["message"]


def test_stepping_streams_progress_then_the_turn(client, sessions, monkeypatch):
    def fake_step(settings, store, session_id, utterance, stream):
        stream.publish("turn", "searching for sources")
        return TURN

    monkeypatch.setattr(conversation, "step", fake_step)
    events = parse(client.post("/v1/session/chat-1/step", json={"utterance": "why?"}).text)

    assert [name for name, _ in events] == ["run", "progress", "done"]
    turn = events[-1][1]
    assert turn["role"] == "Petrologist"
    assert turn["mind_map_reorganised"] is True
    assert turn["citations"][0]["index"] == 1


def test_stepping_without_an_utterance_advances_the_round_table(client, sessions, monkeypatch):
    seen = {}

    def fake_step(settings, store, session_id, utterance, stream):
        seen["utterance"] = utterance
        return TURN

    monkeypatch.setattr(conversation, "step", fake_step)
    client.post("/v1/session/chat-1/step", json={})

    assert seen["utterance"] == ""


def test_stepping_a_missing_session_reports_it_in_the_stream(client, sessions):
    events = parse(client.post("/v1/session/absent/step", json={}).text)

    assert [name for name, _ in events] == ["run", "error"]
    assert "no session absent" in events[-1][1]["message"]


def test_report_streams_markdown_and_citations(client, sessions, monkeypatch):
    def report(settings, store, session_id, stream):
        stream.publish("article", "writing the report")
        return Report(report="# Report", citations=TURN.citations)

    monkeypatch.setattr(conversation, "report", report)

    events = parse(client.post("/v1/session/chat-1/report").text)

    assert events[0][0] == "run"
    assert ("article", "writing the report") in [
        (data["stage"], data["detail"]) for name, data in events if name == "progress"
    ]
    assert events[-1][0] == "done"
    assert events[-1][1]["report"] == "# Report"
    assert events[-1][1]["citations"][0]["url"] == "https://example.org/a"


def test_session_metadata_is_read_from_disk(client, sessions):
    sessions.save(
        "chat-1",
        {
            "runner_argument": {"topic": "The Kvasir stone"},
            "conversation_history": [{}, {}],
            "experts": [{"role_name": "Petrologist"}],
        },
    )

    body = client.get("/v1/session/chat-1").json()

    assert body["topic"] == "The Kvasir stone"
    assert body["turn_count"] == 2
    assert body["experts"] == ["Petrologist"]


def test_a_missing_session_is_404(client, sessions):
    assert client.get("/v1/session/absent").status_code == 404
    assert client.delete("/v1/session/absent").status_code == 404


def test_deleting_a_session_removes_it(client, sessions):
    sessions.save("chat-1", {"runner_argument": {"topic": "x"}, "conversation_history": []})

    assert client.delete("/v1/session/chat-1").status_code == 204
    assert not sessions.exists("chat-1")
