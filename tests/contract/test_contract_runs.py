"""`POST /v1/runs`, and everything that follows a run."""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest
from harness import COST, Fakes, Service, frames, matches_shape, transcript, until

SMALL = {"max_searches": 1, "results_per_query": 1, "max_attempts": 1}
RUNS = {
    "ledger": {**SMALL, "follow_ups": False},
    "ranking": {
        **SMALL,
        "output": "ranking",
        "criteria": ["age"],
        "entities": ["Kvasir stone"],
    },
    "graph": {**SMALL, "max_depth": 1},
    "storm": {"max_conv_turn": 1, "max_perspective": 1, "search_top_k": 1},
}
STAGES = {
    "ledger": ["brief", "research", "write", "verify"],
    "graph": ["brief", "research", "write", "verify"],
    "storm": ["research", "outline", "article", "polish"],
}


def body(name: str, **options: Any) -> dict[str, Any]:
    method = "ledger" if name == "ranking" else name
    return {
        "method": method,
        "input": {"question": "When was the Kvasir stone catalogued?"},
        "options": RUNS[name] | options,
    }


@pytest.fixture
def clean(client: httpx.Client) -> httpx.Client:
    """No page left by another test, so a run reads the same whatever ran before it."""
    assert client.delete("/v1/pages").status_code == 204
    return client


@pytest.mark.parametrize("name", list(RUNS))
def test_a_run_streams_then_is_kept(clean: httpx.Client, name: str) -> None:
    events = transcript(clean, "/v1/runs", json=body(name))

    assert events[0][0] == "run"
    assert events[-1][0] == "done", events[-1]
    assert {event for event, _ in events[1:-1]} == {"progress"}
    matches_shape(f"run.{name}.run", events[0][1])
    matches_shape(f"run.{name}.progress", [data for _, data in events[1:-1]])
    matches_shape(f"run.{name}.done", events[-1][1])

    run_id = events[0][1]["run_id"]
    detail = clean.get(f"/v1/runs/{run_id}").json()
    matches_shape(f"run.{name}.detail", detail)
    assert detail["state"] == "done"
    assert detail["result"] == events[-1][1]
    assert detail["kind"] == body(name)["method"]
    assert detail["ended_at"] >= detail["started_at"]
    assert detail["usage"]["cost"] > 0
    calls = sum(role["calls"] for role in detail["usage"]["by_role"].values())
    assert detail["usage"]["cost"] == pytest.approx(calls * COST)
    assert [run["id"] for run in clean.get("/v1/runs").json()][0] == run_id
    matches_shape(f"run.{name}.summary", clean.get("/v1/runs").json()[0])


@pytest.mark.parametrize("name", ["ledger", "graph", "storm"])
def test_the_stages_of_a_run(clean: httpx.Client, name: str) -> None:
    events = transcript(clean, "/v1/runs", json=body(name))

    detail = clean.get(f"/v1/runs/{events[0][1]['run_id']}").json()
    methods = {info["name"]: info for info in clean.get("/v1/methods").json()}
    assert detail["planned_stages"] == methods[body(name)["method"]]["stages"]
    seen = list(dict.fromkeys(data["stage"] for event, data in events if event == "progress"))
    assert seen == STAGES[name]
    assert [stage["name"] for stage in detail["stages"]] == seen


def test_storm_without_polish_plans_no_polish(clean: httpx.Client) -> None:
    events = transcript(clean, "/v1/runs", json=body("storm", do_polish_article=False))

    detail = clean.get(f"/v1/runs/{events[0][1]['run_id']}").json()
    assert detail["planned_stages"] == ["research", "outline", "article"]
    assert "polish" not in {data["stage"] for event, data in events if event == "progress"}


def test_a_failed_run_ends_in_an_error_frame(clean: httpx.Client, fakes: Fakes) -> None:
    fakes.chat_status = 400
    try:
        events = transcript(clean, "/v1/runs", json=body("ledger"))
    finally:
        fakes.chat_status = None

    assert events[0][0] == "run"
    assert events[-1][0] == "error"
    assert list(events[-1][1]) == ["message"]
    detail = clean.get(f"/v1/runs/{events[0][1]['run_id']}").json()
    assert detail["state"] == "failed"
    assert detail["error"]
    matches_shape("run.failed.detail", detail)


@pytest.mark.parametrize(
    ("payload", "loc", "kind"),
    [
        ({}, ["body"], None),
        ({"method": "nope", "input": {"question": "x"}}, ["body"], "union_tag_invalid"),
        (
            {"method": "ledger", "input": {"question": "x"}, "options": {"nope": 1}},
            ["body", "ledger", "options", "nope"],
            "extra_forbidden",
        ),
        (
            {"method": "ledger", "input": {"question": ""}},
            ["body", "ledger", "input", "question"],
            "string_too_short",
        ),
        (
            {"method": "ledger", "input": {"question": "x"}, "options": {"max_searches": 0}},
            ["body", "ledger", "options", "max_searches"],
            "greater_than_equal",
        ),
    ],
)
def test_a_mistaken_request_is_refused(
    client: httpx.Client, payload: dict[str, Any], loc: list[str], kind: str | None
) -> None:
    before = len(client.get("/v1/runs").json())

    response = client.post("/v1/runs", json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert {"loc", "msg", "type"} <= set(detail[0])
    assert detail[0]["loc"][: len(loc)] == loc
    if kind:
        assert detail[0]["type"] == kind
    assert len(client.get("/v1/runs").json()) == before


def test_a_model_naming_an_unknown_provider_is_refused(client: httpx.Client) -> None:
    response = client.post("/v1/runs", json=body("ledger", model_strong="nope/x"))

    assert response.status_code == 400
    assert "unknown provider 'nope'" in response.json()["message"]


def _start(client: httpx.Client, payload: dict[str, Any]) -> tuple[threading.Thread, list[Any]]:
    """Starts a run on a thread, reading its frames into a list as they come."""
    seen: list[Any] = []

    def read() -> None:
        with client.stream("POST", "/v1/runs", json=payload) as response:
            for item in frames(response.iter_lines()):
                seen.append(item)

    thread = threading.Thread(target=read)
    thread.start()
    return thread, seen


def test_a_full_house_refuses_and_records_it(
    service: Service, client: httpx.Client, fakes: Fakes
) -> None:
    with service.client() as other:
        with fakes.holding_chat():
            thread, seen = _start(other, body("ledger"))
            assert fakes.held.wait(30)

            refused = client.post("/v1/runs", json=body("ledger"))

            assert refused.status_code == 429
            assert refused.json() == {"message": "all run slots are busy, retry later"}
            latest = client.get("/v1/runs").json()[0]
            assert latest["state"] == "rejected"
            matches_shape("run.rejected.summary", latest)
            assert until(lambda: seen)[0][0] == "run"
        thread.join(60)
    assert seen[-1][0] == "done"


def test_a_client_leaving_does_not_end_the_run_or_free_its_slot(
    client: httpx.Client, service: Service, fakes: Fakes
) -> None:
    with service.client() as leaving, fakes.holding_chat():
        with leaving.stream("POST", "/v1/runs", json=body("ledger")) as response:
            run_id = next(frames(response.iter_lines()))[1]["run_id"]
        assert fakes.held.wait(30)

        assert client.post("/v1/runs", json=body("ledger")).status_code == 429
        assert client.get(f"/v1/runs/{run_id}").json()["state"] == "running"

    until(lambda: client.get(f"/v1/runs/{run_id}").json()["state"] == "done", 60)


def test_following_a_live_run(client: httpx.Client, service: Service, fakes: Fakes) -> None:
    follower: list[Any] = []

    def follow(run_id: str) -> None:
        with (
            service.client() as watching,
            watching.stream("GET", f"/v1/runs/{run_id}/events") as response,
        ):
            for item in frames(response.iter_lines()):
                follower.append(item)

    with service.client() as starter:
        with fakes.holding_chat():
            thread, seen = _start(starter, body("ledger"))
            assert fakes.held.wait(30)
            run_id = until(lambda: seen and seen[0][1]["run_id"])
            watcher = threading.Thread(target=follow, args=(run_id,))
            watcher.start()
            until(lambda: follower)
            assert follower[0][0] == "snapshot"
            assert follower[0][1]["ended_at"] is None
        thread.join(60)
    watcher.join(60)

    names = [name for name, _ in follower]
    assert names[-1] == "snapshot"
    assert follower[-1][1]["state"] == "done"
    assert set(names[1:-1]) <= {"progress", "status"}
    assert "progress" in names
    # Each progress event is followed by a status, so usage stays current.
    for index, name in enumerate(names[:-1]):
        if name == "progress":
            assert names[index + 1] == "status"
    matches_shape("events.status", next(data for name, data in follower if name == "status"))


def test_an_ended_run_streams_one_snapshot(clean: httpx.Client) -> None:
    run_id = transcript(clean, "/v1/runs", json=body("ledger"))[0][1]["run_id"]

    events = transcript(clean, f"/v1/runs/{run_id}/events", method="GET")

    assert [name for name, _ in events] == ["snapshot"]
    assert events[0][1] == clean.get(f"/v1/runs/{run_id}").json()


def test_what_a_call_and_a_search_kept(clean: httpx.Client) -> None:
    run_id = transcript(clean, "/v1/runs", json=body("ledger"))[0][1]["run_id"]
    detail = clean.get(f"/v1/runs/{run_id}").json()

    call = clean.get(f"/v1/runs/{run_id}/calls/{detail['calls'][0]['id']}")
    search = clean.get(f"/v1/runs/{run_id}/searches/{detail['searches'][0]['id']}")

    assert call.status_code == search.status_code == 200
    matches_shape("run.call", call.json())
    matches_shape("run.search", search.json())


def test_what_is_not_kept_is_404(client: httpx.Client) -> None:
    assert client.get("/v1/runs/absent").status_code == 404
    assert client.get("/v1/runs/absent").json() == {"message": "no run absent"}
    assert client.get("/v1/runs/absent/events").json() == {"message": "no run absent"}
    assert client.get("/v1/runs/absent/calls/1").status_code == 404
    assert client.get("/v1/runs/absent/searches/1").status_code == 404


def test_a_finished_run_survives_a_restart(new_service: Any, fakes: Fakes) -> None:
    service = new_service()
    with service.client() as client:
        assert client.patch("/v1/settings", json=fakes.settings()).status_code == 200
    service.restart()
    with service.client() as client:
        run_id = transcript(client, "/v1/runs", json=body("ledger"))[0][1]["run_id"]
        before = client.get(f"/v1/runs/{run_id}").json()

    service.restart()

    with service.client() as client:
        assert client.get(f"/v1/runs/{run_id}").json() == before
        assert transcript(client, f"/v1/runs/{run_id}/events", method="GET") == [
            ("snapshot", before)
        ]


def test_a_run_cut_short_by_a_crash_is_marked_failed(new_service: Any, fakes: Fakes) -> None:
    service = new_service()
    with service.client() as client:
        assert client.patch("/v1/settings", json=fakes.settings()).status_code == 200
    service.restart()
    with (
        fakes.holding_chat(),
        service.client() as client,
        client.stream("POST", "/v1/runs", json=body("ledger")) as response,
    ):
        run_id = next(frames(response.iter_lines()))[1]["run_id"]
        assert fakes.held.wait(30)
        service.kill()

    service.start()

    with service.client() as client:
        detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["state"] == "failed"
    assert detail["error"] == "interrupted by a restart"
