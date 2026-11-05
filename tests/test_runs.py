"""The registry records what happened, and the pipeline reports into it without being handed it."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from fakes import AUTH, article_run, stub_method
from kvasir.api.app import app
from kvasir.runs.registry import RunRegistry
from kvasir.runs.run import (
    DONE,
    MAX_CALLS,
    MAX_LOG,
    MAX_READING,
    STATUS_CALLS,
    Run,
    _current_run,
    record_progress,
    record_questions,
)
from kvasir.runs.store import INTERRUPTED, SECONDS_PER_DAY, RunStore
from kvasir.schemas.results import ArticleResult, Report, ResearchQuestion, ResearchResult
from kvasir.schemas.runs import Progress
from kvasir.storm import runtime
from kvasir.storm.runtime import ContextThreadPoolExecutor

ENVIRONMENT = {
    "KVASIR_PROVIDER_GATEWAY_API_KEY": "key",
    "KVASIR_PROVIDER_GATEWAY_API_BASE": "https://gateway.example/v1",
    "KVASIR_MODEL_FAST": "gateway/ollama/fast:cloud",
    "KVASIR_MODEL_STRONG": "gateway/ollama/strong:cloud",
    "KVASIR_MODEL_EMBEDDING": "gateway/embed",
    "KVASIR_SEARCH_URL": "http://searxng.example",
    "KVASIR_TOKEN": "token",
}

RESULT = ArticleResult(article="a", outline="o", citations=[], duration_seconds=1.0)


@pytest.fixture
def client(monkeypatch, tmp_path):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app, headers=AUTH) as client:
        yield client


@pytest.fixture
def sqlite(monkeypatch, tmp_path):
    """Starts the app with runs kept in SQLite. Entering it again is a restart."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KVASIR_RUNS_STORE", "sqlite")
    return lambda: TestClient(app, headers=AUTH)


def parse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        events.append(
            (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        )
    return events


def test_a_run_reaches_done(client, monkeypatch):
    def fake_run(settings, input, options, stream):
        stream.publish("research", "asking questions")
        stream.publish("article", "writing")
        return RESULT

    stub_method(monkeypatch, "storm", fake_run)
    events = parse(client.post("/v1/runs", json=article_run("The Kvasir stone")).text)
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
    def fake_run(settings, input, options, stream):
        raise RuntimeError("gateway refused the connection")

    stub_method(monkeypatch, "storm", fake_run)
    events = parse(client.post("/v1/runs", json=article_run("x")).text)

    detail = client.get(f"/v1/runs/{events[0][1]['run_id']}").json()
    assert detail["state"] == "failed"
    assert detail["error"] == "RuntimeError: gateway refused the connection"


def test_a_rejected_run_is_recorded(client, monkeypatch):
    """A 429 with nothing behind it is exactly the case the registry exists to explain."""
    stub_method(monkeypatch, "storm", lambda *args: RESULT)
    app.state.run_slots.acquire()
    try:
        assert client.post("/v1/runs", json=article_run("x")).status_code == 429
    finally:
        app.state.run_slots.release()

    (summary,) = client.get("/v1/runs").json()
    assert summary["state"] == "rejected"
    assert summary["error"] == "all run slots are busy"


def test_runs_are_listed_newest_first(client, monkeypatch):
    stub_method(monkeypatch, "storm", lambda settings, input, options, stream: RESULT)
    for topic in ("first", "second"):
        client.post("/v1/runs", json=article_run(topic))

    assert [run["topic"] for run in client.get("/v1/runs").json()] == ["second", "first"]


def test_an_unknown_run_is_a_404(client):
    response = client.get("/v1/runs/nope")

    assert response.status_code == 404
    assert response.json() == {"message": "no run nope"}


def test_usage_is_attributed_to_the_running_run(client, monkeypatch):
    """The pipeline reports through a contextvar, so nothing has to hand it the run."""

    def fake_run(settings, input, options, stream):
        runtime.record_lm_usage("openai/strong", "article_gen", 100, 20, 0.002)
        runtime.record_lm_usage("openai/fast", "conv_simulator", 30, 5, 0.0001)
        runtime.record_lm_usage("openai/fast", "conv_simulator", 10, 5, 0.0)
        # Embedding usage is reported straight to the sink, since it arrives on litellm's
        # logging thread where the contextvar is not visible.
        runtime.current_usage_sink().record_embedding("openai/embed", 400)
        runtime.record_search_usage("SearXNG", 3)
        return RESULT

    stub_method(monkeypatch, "storm", fake_run)
    events = parse(client.post("/v1/runs", json=article_run("x")).text)

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

    def fake_run(settings, input, options, stream):
        with ContextThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: runtime.record_lm_usage("m", "r", 10, 1, 0.0), range(8)))
        return RESULT

    stub_method(monkeypatch, "storm", fake_run)
    events = parse(client.post("/v1/runs", json=article_run("x")).text)

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
        run = Run(id="r", kind="storm", topic="t")
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
    stub_method(monkeypatch, "storm", lambda settings, input, options, stream: RESULT)
    events = parse(client.post("/v1/runs", json=article_run("x")).text)

    frames = parse(client.get(f"/v1/runs/{events[0][1]['run_id']}/events").text)

    assert [name for name, _ in frames] == ["snapshot"]
    assert frames[0][1]["state"] == "done"


def test_a_reentered_stage_sums_its_spans():
    """Co-STORM alternates stages, so first entry to last exit would overstate both."""
    run = Run(id="r", kind="costorm", topic="t")
    for stage, at in (("turn", 0.0), ("mind_map", 10.0), ("turn", 12.0), ("mind_map", 20.0)):
        run.record_progress(Progress(stage=stage, detail=stage, at=at))

    stages = {stage.name: stage for stage in run.status().stages}
    assert stages["turn"].seconds == 18.0
    assert len(stages["turn"].spans) == 2
    assert stages["mind_map"].spans[0].ended_at == 12.0


def test_calls_show_in_flight_then_finished_or_failed():
    run = Run(id="r", kind="research", topic="t")
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
    run = Run(id="r", kind="research", topic="t", keep_content=True)
    first = run.start_lm("m", "intake", [{"role": "user", "content": "q"}])
    for _ in range(MAX_CALLS):
        run.start_lm("m", "intake")

    # Forgotten while in flight, so ending it changes nothing.
    run.end_lm(first, 1, 1)

    calls = run.detail().calls
    assert len(calls) == MAX_CALLS
    assert all(call.ended_at is None for call in calls)
    assert run.content(first) is None


def test_a_status_carries_only_the_recent_calls():
    run = Run(id="r", kind="research", topic="t")
    for _ in range(STATUS_CALLS + 5):
        run.start_lm("m", "intake")

    assert [call.id for call in run.status().calls][0] == 5
    assert len(run.detail().calls) == STATUS_CALLS + 5


def test_a_call_is_linked_to_the_event_before_it_in_its_own_work():
    run = Run(id="r", kind="research", topic="t")
    with runtime.record_usage_into(run), _current_run(run):
        runtime.lm_started("m", "brief")
        record_progress(Progress(stage="research", detail="searching"))
        record_progress(Progress(stage="research", detail="reading"))
        with ContextThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(runtime.lm_started, "m", "intake").result()
            # Published after the work was handed out, so that work does not see it.
            record_progress(Progress(stage="research", detail="read"))
        runtime.lm_started("m", "notes")

    assert [event.seq for event in run.detail().events] == [0, 1, 2]
    assert [(call.role, call.event) for call in run.detail().calls] == [
        ("brief", None),
        ("intake", 1),
        ("notes", 2),
    ]


def counter(done, total=None, series="sentences", stage="verify"):
    return Progress(stage=stage, detail=f"checked {done}", done=done, total=total, series=series)


def test_a_series_is_one_event_and_flooding_it_keeps_the_steps():
    run = Run(id="r", kind="research", topic="t")
    for i in range(3):
        run.record_progress(Progress(stage="research", detail=f"s{i}", kind="attempt"))
    first = counter(0, 6000)
    run.record_progress(first)
    for done in range(1, 5001):
        run.record_progress(counter(done, 6000))

    events = run.detail().events
    series = [event for event in events if event.series]
    assert [event.kind for event in events].count("attempt") == 3
    assert len(series) == 1
    assert (series[0].seq, series[0].done, series[0].at) == (3, 5000, first.at)
    assert series[0].updated_at is not None
    assert run.detail().events[-1].seq == 3


def test_a_finished_series_starts_a_new_event_on_its_next_update():
    run = Run(id="r", kind="research", topic="t")
    run.record_progress(counter(0, 2))
    run.record_progress(counter(2, 2))
    again = counter(0, 3)
    run.record_progress(again)
    run.record_progress(counter(1, 3))

    series = [event for event in run.detail().events if event.series]
    assert [(event.seq, event.done, event.total) for event in series] == [(0, 2, 2), (1, 1, 3)]
    assert again.seq == 1


def test_a_call_after_a_series_update_is_linked_to_its_shared_seq():
    run = Run(id="r", kind="research", topic="t")
    with runtime.record_usage_into(run), _current_run(run):
        record_progress(counter(0, 5))
        record_progress(Progress(stage="verify", detail="other"))
        update = counter(1, 5)
        record_progress(update)
        runtime.lm_started("m", "verify")

    assert update.seq == 0
    assert [call.event for call in run.detail().calls] == [0]


def test_each_class_of_event_is_evicted_on_its_own():
    run = Run(id="r", kind="research", topic="t")
    run.record_progress(Progress(stage="research", detail="a", kind="attempt"))
    for i in range(MAX_READING + 5):
        run.record_progress(Progress(stage="research", detail=f"p{i}", kind="page"))
    for i in range(MAX_LOG + 5):
        run.record_progress(Progress(stage="research", detail=f"l{i}"))

    events = run.detail().events
    kinds = [event.kind for event in events]
    assert (kinds.count("attempt"), kinds.count("page"), kinds.count(None)) == (
        1,
        MAX_READING,
        MAX_LOG,
    )
    assert [event.seq for event in events] == sorted(event.seq for event in events)


def test_latest_is_the_updated_counter_even_when_later_events_exist():
    run = Run(id="r", kind="research", topic="t")
    run.record_progress(counter(0, 5))
    run.record_progress(Progress(stage="verify", detail="later", kind="attempt"))
    run.record_progress(counter(3, 5))

    latest = run.summary().latest
    assert (latest.seq, latest.done) == (0, 3)


def test_call_content_is_kept_only_when_asked():
    messages = [{"role": "user", "content": "q"}]
    for keep in (False, True):
        run = Run(id="r", kind="research", topic="t", keep_content=keep)
        call = run.start_lm("m", "intake", messages)
        run.end_lm(call, 10, 2, "an answer", "thinking")

        content = run.content(call)
        assert run.detail().calls[0].has_content is keep
        if keep:
            assert content is not None
            assert (content.messages, content.response, content.reasoning) == (
                messages,
                "an answer",
                "thinking",
            )
        else:
            assert content is None


def test_answered_calls_add_their_time_to_usage(monkeypatch):
    clock = iter([0.0, 2.0, 2.0, 3.0, 10.0])
    monkeypatch.setattr("kvasir.runs.run.time.time", lambda: next(clock))
    run = Run(id="r", kind="research", topic="t")
    answered = run.start_lm("m", "intake")
    run.end_lm(answered, 10, 40)
    failed = run.start_lm("m", "intake")
    run.end_lm(failed, None, None)

    assert run._usage().by_model["m"].seconds == 2.0


def test_usage_is_split_by_the_stage_it_was_spent_in(client, monkeypatch):
    def fake_run(settings, input, options, stream):
        runtime.record_lm_usage("m", "conv_simulator", 30, 5, 0.0)
        stream.publish("research", "asking questions")
        runtime.record_lm_usage("m", "conv_simulator", 10, 5, 0.001)
        runtime.record_search_usage("SearXNG", 2)
        stream.publish("article", "writing")
        runtime.record_lm_usage("m", "article_gen", 100, 40, 0.002, reasoning_tokens=25)
        runtime.current_usage_sink().record_embedding("e", 300)
        return RESULT

    stub_method(monkeypatch, "storm", fake_run)
    events = parse(client.post("/v1/runs", json=article_run("x")).text)

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
    def fake_run(settings, input, options, stream):
        stream.publish("article", "finished a (1/2)", 1, 2)
        return RESULT

    stub_method(monkeypatch, "storm", fake_run)
    events = parse(client.post("/v1/runs", json=article_run("x", do_polish_article=False)).text)

    detail = client.get(f"/v1/runs/{events[0][1]['run_id']}").json()
    assert detail["result"]["article"] == RESULT.article
    assert detail["planned_stages"] == ["research", "outline", "article"]
    assert (detail["latest"]["done"], detail["latest"]["total"]) == (1, 2)
    # The list stays light: the result is only on the detail.
    assert "result" not in client.get("/v1/runs").json()[0]


def test_a_watcher_with_a_heartbeat_yields_when_idle():
    """What lets a follower refresh usage during a stage that publishes nothing."""

    async def scenario():
        run = Run(id="r", kind="storm", topic="t")
        watcher = run.watch(heartbeat=0.01)
        received = []
        async for event in watcher:
            received.append(event)
            if len(received) == 2:
                run._finish(DONE, None)
        return received

    assert asyncio.run(scenario()) == [None, None]


def test_the_registry_forgets_the_oldest():
    registry = RunRegistry(limit=2)
    first = registry.start("storm", "one")
    registry.start("storm", "two")
    registry.start("storm", "three")

    assert registry.get(first.id) is None
    assert [run.topic for run in registry.summaries()] == ["three", "two"]


def test_memory_mode_writes_no_database(client, tmp_path):
    assert not (tmp_path / "data" / "kvasir.db").exists()


def test_a_run_survives_a_restart(sqlite, monkeypatch):
    stub_method(monkeypatch, "storm", lambda *args: RESULT)
    with sqlite() as client:
        events = parse(client.post("/v1/runs", json=article_run("The Kvasir stone")).text)
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


def _article_with_a_call(*args):
    call = runtime.lm_started("m", "article_gen", [{"role": "user", "content": "write"}])
    runtime.lm_ended(call, 10, 2, "an article")
    return RESULT


def test_call_content_is_served_from_memory_and_after_a_restart(sqlite, monkeypatch):
    monkeypatch.setenv("KVASIR_RUNS_KEEP_CALL_CONTENT", "true")
    stub_method(monkeypatch, "storm", _article_with_a_call)
    with sqlite() as client:
        events = parse(client.post("/v1/runs", json=article_run("The Kvasir stone")).text)
        run_id = events[0][1]["run_id"]
        (call,) = client.get(f"/v1/runs/{run_id}").json()["calls"]
        assert call["has_content"] is True
        live = client.get(f"/v1/runs/{run_id}/calls/{call['id']}").json()
        assert live["response"] == "an article"

    with sqlite() as client:
        assert client.get(f"/v1/runs/{run_id}/calls/{call['id']}").json() == live
        assert client.get(f"/v1/runs/{run_id}/calls/99").status_code == 404


class _SearxngAnswer:
    def json(self):
        return {
            "results": [{"url": "https://example.invalid/a", "title": "A", "content": "about a"}]
        }


def _article_with_a_search(*args):
    from kvasir.storm.rm import SearXNG

    record_progress(Progress(stage="research", detail="searching"))
    SearXNG(searxng_api_url="http://searxng.invalid")("the stone")
    return RESULT


def test_search_results_are_served_from_memory_and_after_a_restart(sqlite, monkeypatch):
    monkeypatch.setenv("KVASIR_RUNS_KEEP_CALL_CONTENT", "true")
    monkeypatch.setattr("kvasir.storm.rm.httpx.get", lambda url, **kwargs: _SearxngAnswer())
    stub_method(monkeypatch, "storm", _article_with_a_search)
    with sqlite() as client:
        events = parse(client.post("/v1/runs", json=article_run("The Kvasir stone")).text)
        run_id = events[0][1]["run_id"]
        (search,) = client.get(f"/v1/runs/{run_id}").json()["searches"]
        assert (search["query"], search["event"], search["results"]) == ("the stone", 0, 1)
        assert search["has_content"] is True
        live = client.get(f"/v1/runs/{run_id}/searches/{search['id']}").json()
        assert live == {
            "results": [
                {
                    "title": "A",
                    "url": "https://example.invalid/a",
                    "snippet": "about a",
                    "source": None,
                }
            ]
        }

    with sqlite() as client:
        assert client.get(f"/v1/runs/{run_id}/searches/{search['id']}").json() == live
        assert client.get(f"/v1/runs/{run_id}/searches/99").status_code == 404


def test_search_results_are_not_kept_by_default(client, monkeypatch):
    monkeypatch.setattr("kvasir.storm.rm.httpx.get", lambda url, **kwargs: _SearxngAnswer())
    stub_method(monkeypatch, "storm", _article_with_a_search)
    events = parse(client.post("/v1/runs", json=article_run("The Kvasir stone")).text)
    run_id = events[0][1]["run_id"]

    (search,) = client.get(f"/v1/runs/{run_id}").json()["searches"]
    assert search["has_content"] is False
    assert search["results"] == 1
    assert client.get(f"/v1/runs/{run_id}/searches/{search['id']}").status_code == 404


def test_a_failed_search_says_why():
    run = Run(id="r", kind="research", topic="t")
    search = run.start_search("SearXNG", "q")
    assert run.status().searches[0].ended_at is None

    run.end_search(search, [], ["brave: Suspended: too many requests"])

    (record,) = run.status().searches
    assert (record.results, record.failures) == (0, ["brave: Suspended: too many requests"])


def test_a_status_carries_the_questions_asked_so_far():
    run = Run(id="r", kind="graph", topic="t")
    question = ResearchQuestion(
        id=1, text="q", parent=None, depth=0, status="open", claims=[], queries=[], missing=""
    )
    with _current_run(run):
        record_questions([question])

    assert run.status().questions == [question]


def test_call_content_is_not_kept_by_default(client, monkeypatch):
    stub_method(monkeypatch, "storm", _article_with_a_call)
    events = parse(client.post("/v1/runs", json=article_run("The Kvasir stone")).text)
    run_id = events[0][1]["run_id"]

    (call,) = client.get(f"/v1/runs/{run_id}").json()["calls"]
    assert call["has_content"] is False
    assert call["prompt_tokens"] == 10
    assert client.get(f"/v1/runs/{run_id}/calls/{call['id']}").status_code == 404


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
    store = RunStore(tmp_path / "kvasir.db", ttl_days=30, limit=10)
    run = RunRegistry(10, store).start("research", "t")
    run.set_result(result)
    run._finish(DONE, None)

    assert type(store.detail(run.id).result) is type(result)


def test_a_run_cut_short_by_a_restart_is_marked_interrupted(tmp_path):
    path = tmp_path / "kvasir.db"
    run = RunRegistry(10, RunStore(path, ttl_days=30, limit=10)).start("storm", "t")

    store = RunStore(path, ttl_days=30, limit=10)
    store.sweep()

    (summary,) = store.summaries()
    assert summary.state == "failed"
    assert summary.error == INTERRUPTED
    assert summary.ended_at == summary.created_at
    assert store.detail(run.id).state == "failed"


def test_the_store_keeps_only_what_retention_allows(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "kvasir.db", ttl_days=1, limit=2)
    registry = RunRegistry(10, store)
    old = registry.start("storm", "old")
    old.created_at -= 2 * SECONDS_PER_DAY
    old._finish(DONE, None)
    for topic in ("one", "two", "three"):
        registry.start(topic=topic, kind="storm")._finish(DONE, None)

    assert [summary.topic for summary in store.summaries()] == ["three", "two"]
    assert store.detail(old.id) is None
