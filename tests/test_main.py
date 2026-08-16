"""Route behaviour with the STORM run faked. No network, no model calls."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from kvasir import main
from kvasir.models import Citation, ResearchResult

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
def environment(monkeypatch):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


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
    assert [name for name, _ in events] == ["progress", "progress", "done"]
    assert events[0][1] == {"stage": "research", "detail": "asking questions"}
    assert events[-1][1]["article"] == RESULT.article
    assert events[-1][1]["citations"][0]["index"] == 1


def test_research_reports_a_failure_as_an_error_event(client, monkeypatch):
    def fake_run(settings, request, stream):
        raise RuntimeError("gateway refused the connection")

    monkeypatch.setattr(main, "run_research", fake_run)
    events = parse(client.post("/v1/research", json={"topic": "x"}).text)

    assert [name for name, _ in events] == ["error"]
    assert "gateway refused the connection" in events[0][1]["message"]


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
