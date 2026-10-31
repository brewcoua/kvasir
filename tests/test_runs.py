"""The registry records what happened, and the pipeline reports into it without being handed it."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from kvasir import main, run_store, runs
from kvasir.models import ArticleResult, Progress, Report, ResearchResult
from kvasir.storm import runtime
from kvasir.storm.runtime import ContextThreadPoolExecutor

ENVIRONMENT = {
    "OPENAI_API_KEY": "key",
    "OPENAI_API_BASE": "https://gateway.example/v1",
    "KVASIR_MODEL_FAST": "openai/ollama/fast:cloud",
    "KVASIR_MODEL_STRONG": "openai/ollama/strong:cloud",
    "KVASIR_SEARXNG_URL": "http://searxng.example",
}

RESULT = ArticleResult(article="a", outline="o", citations=[], duration_seconds=1.0)


@pytest.fixture
def client(monkeypatch, tmp_path):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def sqlite(monkeypatch, tmp_path):
    """Starts the app with runs kept in SQLite. Entering it again is a restart."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KVASIR_RUN_STORE", "sqlite")
    return lambda: TestClient(main.app)


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

    monkeypatch.setattr(main, "run_article", fake_run)
    events = parse(client.post("/v1/article", json={"topic": "The Kvasir stone"}).text)
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

    monkeypatch.setattr(main, "run_article", fake_run)
    events = parse(client.post("/v1/article", json={"topic": "x"}).text)

    detail = client.get(f"/v1/runs/{events[0][1]['run_id']}").json()
    assert detail["state"] == "failed"
    assert detail["error"] == "RuntimeError: gateway refused the connection"


def test_a_rejected_run_is_recorded(client, monkeypatch):
    """A 429 with nothing behind it is exactly the case the registry exists to explain."""
    monkeypatch.setattr(main, "run_article", lambda *args: RESULT)
    main.app.state.run_slots.acquire()
    try:
        assert client.post("/v1/article", json={"topic": "x"}).status_code == 429
    finally:
        main.app.state.run_slots.release()

    (summary,) = client.get("/v1/runs").json()
    assert summary["state"] == "rejected"
    assert summary["error"] == "all run slots are busy"


def test_runs_are_listed_newest_first(client, monkeypatch):
    monkeypatch.setattr(main, "run_article", lambda settings, request, stream: RESULT)
    for topic in ("first", "second"):
        client.post("/v1/article", json={"topic": topic})

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

    monkeypatch.setattr(main, "run_article", fake_run)
    events = parse(client.post("/v1/article", json={"topic": "x"}).text)

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

    monkeypatch.setattr(main, "run_article", fake_run)
    events = parse(client.post("/v1/article", json={"topic": "x"}).text)

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
                run.record_progress(Progress(stage="research", detail="asking questions"))
                run.record_progress(Progress(stage="article", detail="writing"))

        worker = threading.Thread(target=work)
        worker.start()
        # Ends when the run reaches a terminal state, which is what closes the stream.
        await asyncio.wait_for(task, 5)
        worker.join()
        return received

    assert asyncio.run(scenario()) == ["asking questions", "writing"]


def test_watching_a_finished_run_does_not_hang(client, monkeypatch):
    monkeypatch.setattr(main, "run_article", lambda settings, request, stream: RESULT)
    events = parse(client.post("/v1/article", json={"topic": "x"}).text)

    frames = parse(client.get(f"/v1/runs/{events[0][1]['run_id']}/events").text)

    assert [name for name, _ in frames] == ["snapshot"]
    assert frames[0][1]["state"] == "done"


def test_a_reentered_stage_sums_its_spans():
    """Co-STORM alternates stages, so first entry to last exit would overstate both."""
    run = runs.Run(id="r", kind="costorm", topic="t")
    for stage, at in (("turn", 0.0), ("mind_map", 10.0), ("turn", 12.0), ("mind_map", 20.0)):
        run.record_progress(Progress(stage=stage, detail=stage, at=at))

    stages = {stage.name: stage for stage in run.status().stages}
    assert stages["turn"].seconds == 18.0
    assert len(stages["turn"].spans) == 2
    assert stages["mind_map"].spans[0].ended_at == 12.0


def test_calls_show_in_flight_then_finished_or_failed():
    run = runs.Run(id="r", kind="research", topic="t")
    answered = run.start_lm("openai/fast", "intake")
    failed = run.start_lm("openai/fast", "intake")
    run.start_lm("openai/strong", "write")

    run.end_lm(answered, 100, 20)
    run.end_lm(failed, None, None)

    calls = run.status().calls
    assert [(c.role, c.prompt_tokens, c.failed) for c in calls] == [
        ("intake", 100, False),
        ("intake", None, True),
        ("write", None, False),
    ]
    assert calls[0].ended_at is not None
    assert calls[2].ended_at is None


def test_only_the_most_recent_calls_are_kept():
    run = runs.Run(id="r", kind="research", topic="t")
    first = run.start_lm("m", "intake")
    for _ in range(runs.MAX_CALLS):
        run.start_lm("m", "intake")

    # Forgotten while in flight, so ending it changes nothing.
    run.end_lm(first, 1, 1)

    assert len(run.status().calls) == runs.MAX_CALLS
    assert all(call.ended_at is None for call in run.status().calls)


def test_usage_is_split_by_the_stage_it_was_spent_in(client, monkeypatch):
    def fake_run(settings, request, stream):
        runtime.record_lm_usage("m", "conv_simulator", 30, 5, 0.0)
        stream.publish("research", "asking questions")
        runtime.record_lm_usage("m", "conv_simulator", 10, 5, 0.001)
        runtime.record_search_usage("SearXNG", 2)
        stream.publish("article", "writing")
        runtime.record_lm_usage("m", "article_gen", 100, 40, 0.002, reasoning_tokens=25)
        runtime.current_usage_sink().record_embedding("e", 300)
        return RESULT

    monkeypatch.setattr(main, "run_article", fake_run)
    events = parse(client.post("/v1/article", json={"topic": "x"}).text)

    usage = client.get(f"/v1/runs/{events[0][1]['run_id']}").json()["usage"]
    by_stage = usage["by_stage"]
    assert by_stage["unstaged"]["prompt_tokens"] == 30
    assert by_stage["research"]["prompt_tokens"] == 10
    assert by_stage["research"]["searches"] == 2
    assert by_stage["article"]["completion_tokens"] == 40
    assert by_stage["article"]["reasoning_tokens"] == 25
    assert by_stage["article"]["embedding_tokens"] == 300
    assert usage["reasoning_tokens"] == 25


def test_a_finished_run_carries_its_result_and_plan(client, monkeypatch):
    def fake_run(settings, request, stream):
        stream.publish("article", "finished a (1/2)", 1, 2)
        return RESULT

    monkeypatch.setattr(main, "run_article", fake_run)
    events = parse(client.post("/v1/article", json={"topic": "x", "do_polish_article": False}).text)

    detail = client.get(f"/v1/runs/{events[0][1]['run_id']}").json()
    assert detail["result"]["article"] == RESULT.article
    assert detail["planned_stages"] == ["research", "outline", "article"]
    assert (detail["latest"]["done"], detail["latest"]["total"]) == (1, 2)
    # The list stays light: the result is only on the detail.
    assert "result" not in client.get("/v1/runs").json()[0]


def test_a_watcher_with_a_heartbeat_yields_when_idle():
    """What lets a follower refresh usage during a stage that publishes nothing."""

    async def scenario():
        run = runs.Run(id="r", kind="storm", topic="t")
        watcher = run.watch(heartbeat=0.01)
        received = []
        async for event in watcher:
            received.append(event)
            if len(received) == 2:
                run._finish(runs.DONE, None)
        return received

    assert asyncio.run(scenario()) == [None, None]


def test_the_registry_forgets_the_oldest():
    registry = runs.RunRegistry(limit=2)
    first = registry.start("storm", "one")
    registry.start("storm", "two")
    registry.start("storm", "three")

    assert registry.get(first.id) is None
    assert [run.topic for run in registry.summaries()] == ["three", "two"]


def test_the_page_is_self_contained(client):
    """The image is read-only and offline, so anything from another origin would never load."""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    body = response.text
    assert "<title>kvasir runs</title>" in body
    for origin in ("http://", "https://", "//cdn", "src=", "@import"):
        assert origin not in body, origin


def test_memory_mode_writes_no_database(client, tmp_path):
    assert not (tmp_path / "data" / "kvasir.db").exists()


def test_a_run_survives_a_restart(sqlite, monkeypatch):
    monkeypatch.setattr(main, "run_article", lambda *args: RESULT)
    with sqlite() as client:
        events = parse(client.post("/v1/article", json={"topic": "The Kvasir stone"}).text)
        run_id = events[0][1]["run_id"]

    with sqlite() as client:
        (summary,) = client.get("/v1/runs").json()
        assert summary["id"] == run_id
        assert summary["state"] == "done"
        assert summary["has_result"] is True

        detail = client.get(f"/v1/runs/{run_id}").json()
        assert detail["result"]["article"] == "a"

        # A stored run has ended, so following it gives the one snapshot.
        (snapshot,) = parse(client.get(f"/v1/runs/{run_id}/events").text)
        assert snapshot[0] == "snapshot"
        assert snapshot[1]["id"] == run_id

        assert client.get("/v1/runs/missing").status_code == 404


@pytest.mark.parametrize(
    "result",
    [
        ResearchResult(
            report="r", assumptions=["a"], citations=[], claims=[], duration_seconds=1.0
        ),
        Report(report="r", citations=[]),
    ],
)
def test_a_stored_result_keeps_its_type(tmp_path, result):
    """`RunResult` is an untagged union, and these two share their `report` field."""
    store = run_store.RunStore(tmp_path / "kvasir.db", ttl_days=30, limit=10)
    run = runs.RunRegistry(10, store).start("research", "t")
    run.set_result(result)
    run._finish(runs.DONE, None)

    assert type(store.detail(run.id).result) is type(result)


def test_a_run_cut_short_by_a_restart_is_marked_interrupted(tmp_path):
    path = tmp_path / "kvasir.db"
    run = runs.RunRegistry(10, run_store.RunStore(path, ttl_days=30, limit=10)).start("storm", "t")

    store = run_store.RunStore(path, ttl_days=30, limit=10)
    store.sweep()

    (summary,) = store.summaries()
    assert summary.state == "failed"
    assert summary.error == run_store.INTERRUPTED
    assert summary.ended_at == summary.created_at
    assert store.detail(run.id).state == "failed"


def test_the_store_keeps_only_what_retention_allows(tmp_path, monkeypatch):
    store = run_store.RunStore(tmp_path / "kvasir.db", ttl_days=1, limit=2)
    registry = runs.RunRegistry(10, store)
    old = registry.start("storm", "old")
    old.created_at -= 2 * run_store.SECONDS_PER_DAY
    old._finish(runs.DONE, None)
    for topic in ("one", "two", "three"):
        registry.start(topic=topic, kind="storm")._finish(runs.DONE, None)

    assert [summary.topic for summary in store.summaries()] == ["three", "two"]
    assert store.detail(old.id) is None
