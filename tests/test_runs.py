"""The registry records what happened, and the pipeline reports into it without being handed it."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from kvasir import main, runs
from kvasir.models import ResearchResult
from kvasir.storm import runtime
from kvasir.storm.runtime import ContextThreadPoolExecutor

ENVIRONMENT = {
    "OPENAI_API_KEY": "key",
    "OPENAI_API_BASE": "https://gateway.example/v1",
    "KVASIR_MODEL_FAST": "openai/ollama/fast:cloud",
    "KVASIR_MODEL_STRONG": "openai/ollama/strong:cloud",
    "KVASIR_SEARXNG_URL": "http://searxng.example",
}

RESULT = ResearchResult(article="a", outline="o", citations=[], duration_seconds=1.0)


@pytest.fixture
def client(monkeypatch, tmp_path):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    with TestClient(main.app) as client:
        yield client


def parse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        events.append(
            (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        )
    return events


def test_a_run_reaches_done(client, monkeypatch):
    def fake_run(settings, request, stream):
        stream.publish("research", "asking questions")
        stream.publish("article", "writing")
        return RESULT

    monkeypatch.setattr(main, "run_research", fake_run)
    events = parse(client.post("/v1/research", json={"topic": "The Kvasir stone"}).text)
    run_id = events[0][1]["run_id"]

    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["state"] == "done"
    assert detail["topic"] == "The Kvasir stone"
    assert detail["kind"] == "storm"
    assert [stage["name"] for stage in detail["stages"]] == ["research", "article"]
    # Every stage is closed once the run ends, including the one it was in when it finished.
    assert all(stage["ended_at"] is not None for stage in detail["stages"])
    assert [event["detail"] for event in detail["events"]] == ["asking questions", "writing"]


def test_a_failed_run_keeps_the_reason(client, monkeypatch):
    def fake_run(settings, request, stream):
        raise RuntimeError("gateway refused the connection")

    monkeypatch.setattr(main, "run_research", fake_run)
    events = parse(client.post("/v1/research", json={"topic": "x"}).text)

    detail = client.get(f"/v1/runs/{events[0][1]['run_id']}").json()
    assert detail["state"] == "failed"
    assert detail["error"] == "RuntimeError: gateway refused the connection"


def test_a_rejected_run_is_recorded(client, monkeypatch):
    """A 429 with nothing behind it is exactly the case the registry exists to explain."""
    monkeypatch.setattr(main, "run_research", lambda *args: RESULT)
    main.app.state.run_slots.acquire()
    try:
        assert client.post("/v1/research", json={"topic": "x"}).status_code == 429
    finally:
        main.app.state.run_slots.release()

    (summary,) = client.get("/v1/runs").json()
    assert summary["state"] == "rejected"
    assert summary["error"] == "all run slots are busy"


def test_runs_are_listed_newest_first(client, monkeypatch):
    monkeypatch.setattr(main, "run_research", lambda settings, request, stream: RESULT)
    for topic in ("first", "second"):
        client.post("/v1/research", json={"topic": topic})

    assert [run["topic"] for run in client.get("/v1/runs").json()] == ["second", "first"]


def test_an_unknown_run_is_a_404(client):
    response = client.get("/v1/runs/nope")

    assert response.status_code == 404
    assert response.json() == {"message": "no run nope"}


def test_usage_is_attributed_to_the_running_run(client, monkeypatch):
    """The pipeline reports through a contextvar, so nothing has to hand it the run."""

    def fake_run(settings, request, stream):
        runtime.record_lm_usage("openai/strong", "article_gen", 100, 20, 0.002)
        runtime.record_lm_usage("openai/fast", "conv_simulator", 30, 5, 0.0001)
        runtime.record_lm_usage("openai/fast", "conv_simulator", 10, 5, 0.0)
        # Embedding usage is reported straight to the sink, since it arrives on litellm's
        # logging thread where the contextvar is not visible.
        runtime.current_usage_sink().record_embedding("openai/embed", 400)
        runtime.record_search_usage("SearXNG", 3)
        return RESULT

    monkeypatch.setattr(main, "run_research", fake_run)
    events = parse(client.post("/v1/research", json={"topic": "x"}).text)

    usage = client.get(f"/v1/runs/{events[0][1]['run_id']}").json()["usage"]
    assert usage["prompt_tokens"] == 140
    assert usage["completion_tokens"] == 30
    assert usage["cost"] == pytest.approx(0.0021)
    assert usage["embedding_tokens"] == 400
    assert usage["searches"] == 3
    assert usage["by_model"]["openai/fast"]["calls"] == 2
    assert usage["by_role"]["article_gen"]["prompt_tokens"] == 100


def test_usage_reported_from_a_pool_still_lands_on_the_run(client, monkeypatch):
    """Most of a run's spending happens inside the pipeline's thread pools."""

    def fake_run(settings, request, stream):
        with ContextThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: runtime.record_lm_usage("m", "r", 10, 1, 0.0), range(8)))
        return RESULT

    monkeypatch.setattr(main, "run_research", fake_run)
    events = parse(client.post("/v1/research", json={"topic": "x"}).text)

    usage = client.get(f"/v1/runs/{events[0][1]['run_id']}").json()["usage"]
    assert usage["by_model"]["m"]["calls"] == 8


def test_usage_outside_a_run_goes_nowhere():
    """The fork is a library first: reporting with nothing listening must not fail."""
    runtime.record_lm_usage("m", "r", 1, 1, 0.0)
    runtime.record_search_usage("m", 1)
    assert runtime.current_usage_sink() is None


def test_a_watcher_follows_a_run_from_another_thread():
    """The events endpoint reads on the event loop while the pipeline reports from its threads."""

    async def scenario():
        run = runs.Run(id="r", kind="storm", topic="t")
        watcher = run.watch()
        received = []

        async def drain():
            async for event in watcher:
                received.append(event.detail)

        task = asyncio.create_task(drain())
        # Let the drain start before anything is published, as an attaching client would.
        await asyncio.sleep(0)

        def work():
            with run.active():
                run.record_progress("research", "asking questions")
                run.record_progress("article", "writing")

        worker = threading.Thread(target=work)
        worker.start()
        # Ends when the run reaches a terminal state, which is what closes the stream.
        await asyncio.wait_for(task, 5)
        worker.join()
        return received

    assert asyncio.run(scenario()) == ["asking questions", "writing"]


def test_watching_a_finished_run_does_not_hang(client, monkeypatch):
    monkeypatch.setattr(main, "run_research", lambda settings, request, stream: RESULT)
    events = parse(client.post("/v1/research", json={"topic": "x"}).text)

    frames = parse(client.get(f"/v1/runs/{events[0][1]['run_id']}/events").text)

    assert [name for name, _ in frames] == ["snapshot"]
    assert frames[0][1]["state"] == "done"


def test_the_registry_forgets_the_oldest():
    registry = runs.RunRegistry(limit=2)
    first = registry.start("storm", "one")
    registry.start("storm", "two")
    registry.start("storm", "three")

    assert registry.get(first.id) is None
    assert [run.topic for run in registry.list()] == ["three", "two"]


def test_the_page_is_self_contained(client):
    """The image is read-only and offline, so anything from another origin would never load."""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    body = response.text
    assert "<title>kvasir runs</title>" in body
    for origin in ("http://", "https://", "//cdn", "src=", "@import"):
        assert origin not in body, origin
