"""Route behaviour with the STORM run faked. No network, no model calls."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from kvasir import main
from kvasir.models import Citation, Report, ResearchResult, SessionInfo, Turn

ENVIRONMENT = {
    "OPENAI_API_KEY": "key",
    "OPENAI_API_BASE": "https://gateway.example/v1",
    "KVASIR_MODEL_FAST": "openai/ollama/fast:cloud",
    "KVASIR_MODEL_STRONG": "openai/ollama/strong:cloud",
    "KVASIR_SEARXNG_URL": "http://searxng.example",
}

RESULT = ResearchResult(
    article="# Discovery\n\nText [1].",
    outline="# Discovery",
    citations=[Citation(index=1, url="https://example.org/a", title="A", snippet="s")],
    duration_seconds=1.0,
)


@pytest.fixture
def environment(monkeypatch, tmp_path):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    # Startup creates the sessions directory, and the default /data is not writable here.
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def client(environment):
    with TestClient(main.app) as client:
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


def test_startup_fails_loudly_on_missing_configuration(monkeypatch):
    for name in ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(Exception, match="OPENAI_API_KEY"), TestClient(main.app):
        pass


@pytest.mark.parametrize(
    ("gateway_ok", "searxng_ok", "expected"),
    [(True, True, 200), (False, True, 503), (True, False, 503), (False, False, 503)],
)
def test_readyz_hard_fails_when_either_dependency_is_down(
    client, monkeypatch, gateway_ok, searxng_ok, expected
):
    async def reachable(url, headers=None):
        return gateway_ok if "gateway" in url else searxng_ok

    monkeypatch.setattr(main, "_reachable", reachable)
    response = client.get("/readyz")

    assert response.status_code == expected
    assert response.json() == {"gateway": gateway_ok, "searxng": searxng_ok}


def test_readyz_treats_an_unreachable_dependency_as_not_ready(client, monkeypatch):
    async def refuse(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx.AsyncClient, "get", refuse)

    assert client.get("/readyz").status_code == 503


def test_research_streams_progress_then_the_article(client, monkeypatch):
    def fake_run(settings, request, stream):
        stream.publish("research", "asking questions")
        stream.publish("article", "writing sections")
        return RESULT

    monkeypatch.setattr(main, "run_research", fake_run)
    response = client.post("/v1/research", json={"topic": "The Kvasir stone"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse(response.text)
    assert [name for name, _ in events] == ["run", "progress", "progress", "done"]
    assert events[0][1]["run_id"]
    assert events[1][1] == {"stage": "research", "detail": "asking questions"}
    assert events[-1][1]["article"] == RESULT.article
    assert events[-1][1]["citations"][0]["index"] == 1


def test_research_reports_a_failure_as_an_error_event(client, monkeypatch):
    def fake_run(settings, request, stream):
        raise RuntimeError("gateway refused the connection")

    monkeypatch.setattr(main, "run_research", fake_run)
    events = parse(client.post("/v1/research", json={"topic": "x"}).text)

    assert [name for name, _ in events] == ["run", "error"]
    assert "gateway refused the connection" in events[-1][1]["message"]


def test_research_rejects_an_empty_topic(client):
    assert client.post("/v1/research", json={"topic": ""}).status_code == 422


def test_a_slot_is_released_after_every_run(client, monkeypatch):
    monkeypatch.setattr(main, "run_research", lambda *args: RESULT)

    for _ in range(3):
        assert client.post("/v1/research", json={"topic": "x"}).status_code == 200


def test_saturation_returns_429_rather_than_queueing(client, monkeypatch):
    monkeypatch.setattr(main, "run_research", lambda *args: RESULT)
    # Standing in for a run already in flight. TestClient buffers a streaming response, so an
    # actual concurrent request cannot be held open here.
    assert main.app.state.run_slots.acquire(blocking=False)

    try:
        response = client.post("/v1/research", json={"topic": "second"})
    finally:
        main.app.state.run_slots.release()

    assert response.status_code == 429
    assert "busy" in response.json()["message"]
    # Refusal must not consume a slot, or the service deadlocks after the first refusal.
    assert client.post("/v1/research", json={"topic": "third"}).status_code == 200


@pytest.fixture
def sessions(client):
    """The store the running app is actually using, under the temporary data directory."""
    return main.app.state.sessions


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

    monkeypatch.setattr(main.conversation, "create", fake_create)
    events = parse(client.post("/v1/session", json={"session_id": "chat-1", "topic": "x"}).text)

    assert [name for name, _ in events] == ["run", "progress", "done"]
    assert events[1][1]["stage"] == "warm_start"
    assert events[-1][1]["experts"] == ["Petrologist"]


def test_creating_a_session_twice_is_refused(client, sessions, monkeypatch):
    monkeypatch.setattr(main.conversation, "create", lambda *args: INFO)
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

    monkeypatch.setattr(main.conversation, "step", fake_step)
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

    monkeypatch.setattr(main.conversation, "step", fake_step)
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

    monkeypatch.setattr(main.conversation, "report", report)

    events = parse(client.post("/v1/session/chat-1/report").text)

    assert events[0][0] == "run"
    assert ("progress", {"stage": "article", "detail": "writing the report"}) in events
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
