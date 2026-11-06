"""Health, auth, first start, settings, methods, providers and the one-call routes."""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

import httpx
import pytest
from harness import TOKEN, Fakes, Service, matches_shape, transcript, until

GUARDED = [
    ("GET", "/v1/runs"),
    ("POST", "/v1/runs"),
    ("GET", "/v1/runs/x"),
    ("GET", "/v1/runs/x/events"),
    ("GET", "/v1/runs/x/calls/1"),
    ("GET", "/v1/runs/x/searches/1"),
    ("GET", "/v1/methods"),
    ("POST", "/v1/clarify"),
    ("POST", "/v1/tasks"),
    ("GET", "/v1/settings"),
    ("PATCH", "/v1/settings"),
    ("DELETE", "/v1/settings/x"),
    ("GET", "/v1/providers"),
    ("GET", "/v1/search/challenges"),
    ("GET", "/v1/providers/x/models"),
    ("GET", "/v1/pages"),
    ("DELETE", "/v1/pages"),
    ("GET", "/v1/pages/stats"),
    ("GET", "/v1/pages/recall"),
    ("GET", "/v1/pages/x"),
    ("DELETE", "/v1/pages/x"),
    ("POST", "/v1/pages/x/refetch"),
    ("GET", "/v1/sites"),
    ("GET", "/v1/sites/graph"),
    ("DELETE", "/v1/sites/x"),
    ("GET", "/v1/favicons/x"),
    ("DELETE", "/v1/favicons"),
    ("GET", "/v1/sources/x"),
    ("DELETE", "/v1/sources"),
    ("PUT", "/v1/sources/x/mark"),
    ("DELETE", "/v1/sources/x/mark"),
]


def test_health_needs_no_token(service: Service) -> None:
    with service.client(headers={}) as anonymous:
        assert anonymous.get("/healthz").json() == {"status": "ok"}
        assert anonymous.get("/readyz").json() == {
            "providers": {"gateway": True},
            "search": {"SearXNG": {"state": "ok"}},
        }
        # Built or not, the page itself is not behind the token.
        assert anonymous.get("/").status_code in (200, 503)


def test_each_request_is_logged(client: httpx.Client, service: Service) -> None:
    assert client.get("/v1/runs/absent").status_code == 404

    line = until(
        lambda: [line for line in service.log.read_text().splitlines() if "/v1/runs/absent" in line]
    )[-1]
    assert "GET" in line and "404" in line


def test_readiness_fails_with_what_is_unreachable(new_service: Any, fakes: Fakes) -> None:
    service = new_service()
    with service.client() as client:
        changes = fakes.settings() | {"search.searxng.url": "http://127.0.0.1:9"}
        assert client.patch("/v1/settings", json=changes).status_code == 200

        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "providers": {"gateway": True},
        "search": {"SearXNG": {"state": "unreachable"}},
    }


@pytest.mark.parametrize(("method", "path"), GUARDED)
def test_every_route_needs_the_token(service: Service, method: str, path: str) -> None:
    with service.client(headers={}) as anonymous:
        response = anonymous.request(method, path)

    assert response.status_code == 401
    assert response.json() == {"message": "missing or wrong token"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_wrong_token_is_refused(service: Service) -> None:
    with service.client(headers={"Authorization": "Bearer wrong"}) as client:
        assert client.get("/v1/runs").status_code == 401


def test_login_sets_a_cookie_the_page_can_use(service: Service) -> None:
    with service.client(headers={}) as browser:
        assert browser.post("/v1/login", json={"token": "wrong"}).status_code == 401
        response = browser.post("/v1/login", json={"token": TOKEN})

        assert response.status_code == 204
        cookie = response.headers["set-cookie"].lower()
        assert cookie.startswith(f"kvasir_token={TOKEN.lower()};")
        assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=2592000" in cookie
        assert "secure" not in cookie
        assert browser.get("/v1/runs").status_code == 200


def test_login_needs_a_token_field(service: Service) -> None:
    with service.client(headers={}) as browser:
        response = browser.post("/v1/login", json={})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "token"]


@pytest.mark.parametrize(
    ("site", "allowed"),
    [("same-origin", True), ("none", True), ("same-site", False), ("cross-site", False)],
)
def test_a_cookie_cannot_change_anything_from_another_page(
    service: Service, site: str, allowed: bool
) -> None:
    with service.client(headers={}, cookies={"kvasir_token": TOKEN}) as browser:
        headers = {"Sec-Fetch-Site": site}
        # Reading is not refused: another page cannot read the answer.
        assert browser.get("/v1/runs", headers=headers).status_code == 200
        response = browser.delete("/v1/favicons", headers=headers)

    assert response.status_code == (204 if allowed else 401)


def test_a_bearer_token_is_not_asked_where_it_came_from(client: httpx.Client) -> None:
    assert (
        client.delete("/v1/favicons", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 204
    )


def test_an_unknown_route_is_404(client: httpx.Client) -> None:
    response = client.get("/v1/nope")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


# -- First start -----------------------------------------------------------------------------------


REQUIRED = ["model.fast", "model.strong", "model.embedding", "search.provider"]
SET_UP = [
    "model.fast",
    "model.strong",
    "model.embedding",
    "search.searxng.enabled",
    "search.searxng.url",
]
BEFORE_SETUP = [
    ("/v1/runs", {"method": "ledger", "input": {"question": "x"}}),
    ("/v1/clarify", {"question": "x"}),
    ("/v1/tasks", {"messages": [{"role": "user", "content": "x"}]}),
]


def test_nothing_runs_before_the_setup(new_service: Any, fakes: Fakes) -> None:
    service = new_service()
    with service.client() as client:
        ready = client.get("/readyz")
        assert ready.status_code == 503
        assert ready.json() == {"setup": "required", "missing": REQUIRED}
        for path, payload in BEFORE_SETUP:
            response = client.post(path, json=payload)
            assert response.status_code == 409, path
            assert response.json() == {
                "message": f"set these settings first: {', '.join(REQUIRED)}"
            }
        assert client.get("/v1/settings").json()["missing"] == REQUIRED
        assert client.get("/v1/runs").json() == []

        changes = {key: fakes.settings()[key] for key in SET_UP}
        answer = client.patch("/v1/settings", json=changes).json()

        assert answer["missing"] == []
        assert client.get("/readyz").status_code == 200


# -- Settings --------------------------------------------------------------------------------------


def _field(view: dict[str, Any], key: str) -> dict[str, Any]:
    return next(field for field in view["fields"] if field["key"] == key)


def test_settings_list_every_field(client: httpx.Client) -> None:
    view = client.get("/v1/settings").json()

    matches_shape("settings.view", view)
    assert not [field["key"] for field in view["fields"] if field["key"].startswith("sessions.")]
    assert sorted(field["key"].partition(".")[0] for field in view["fields"])[0] == "favicons"
    matches_shape("settings.keys", {field["key"]: field["type"] for field in view["fields"]})


def test_a_setting_is_changed_and_reset(client: httpx.Client) -> None:
    changed = client.patch("/v1/settings", json={"storm.max_conv_turn": 5})
    assert changed.status_code == 200
    assert _field(changed.json(), "storm.max_conv_turn")["value"] == 5
    assert _field(client.get("/v1/settings").json(), "storm.max_conv_turn")["value"] == 5

    reset = client.delete("/v1/settings/storm.max_conv_turn")

    assert reset.status_code == 200
    assert _field(reset.json(), "storm.max_conv_turn")["value"] == 3


def test_a_secret_is_written_but_never_returned(client: httpx.Client) -> None:
    key = "search.brave.api_key"
    assert _field(client.get("/v1/settings").json(), key)["value"] == {"set": False}

    changed = client.patch("/v1/settings", json={key: "hidden-value"})
    assert changed.status_code == 200
    assert "hidden-value" not in changed.text
    assert _field(changed.json(), key) | {"help": ""} == {
        "key": key,
        "type": "secret",
        "help": "",
        "value": {"set": True},
        "default": {"set": False},
        "stored": True,
        "required": False,
        "nullable": False,
        "restart": False,
        "minimum": None,
        "choices": None,
    }

    other = client.patch("/v1/settings", json={"storm.max_conv_turn": 5})
    assert _field(other.json(), key)["value"] == {"set": True}
    assert "hidden-value" not in client.get("/v1/settings").text

    refused = client.patch("/v1/settings", json={key: 12345})
    assert refused.status_code == 422
    assert refused.json() == {"message": f"{key} must be a non-empty string"}

    removed = client.delete(f"/v1/settings/{key}")
    assert _field(removed.json(), key)["value"] == {"set": False}
    assert client.delete("/v1/settings/storm.max_conv_turn").status_code == 200


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([1], "send an object of values by key"),
        ({"nope.nope": 1}, "unknown setting nope.nope"),
    ],
)
def test_a_mistaken_change_says_why(client: httpx.Client, payload: Any, message: str) -> None:
    response = client.patch("/v1/settings", json=payload)

    assert response.status_code == 422
    assert response.json() == {"message": message}


def test_an_unusable_value_writes_nothing(client: httpx.Client) -> None:
    before = client.get("/v1/settings").json()

    response = client.patch(
        "/v1/settings", json={"storm.max_perspective": 9, "storm.max_conv_turn": "many"}
    )

    assert response.status_code == 422
    assert list(response.json()) == ["message"]
    assert "storm.max_conv_turn" in response.json()["message"]
    assert client.get("/v1/settings").json() == before


def test_a_setting_read_at_startup_waits_for_a_restart(new_service: Any, fakes: Fakes) -> None:
    service = new_service()
    with service.client() as client:
        view = client.patch("/v1/settings", json={"runs.max": 7}).json()
        assert view["restart_required"] == ["runs.max"]
    service.restart()
    with service.client() as client:
        view = client.get("/v1/settings").json()
    assert view["restart_required"] == []
    assert _field(view, "runs.max")["value"] == 7


def test_a_key_this_version_does_not_know_can_be_deleted(new_service: Any) -> None:
    """On a plain store, which also shows that one is encrypted when the service starts."""
    service = new_service()
    service.stop()
    for file in service.data.glob("kvasir.db*"):
        file.unlink()
    with sqlite3.connect(service.data / "kvasir.db") as db:
        db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO settings VALUES ('retired.option', '168')")
    db.close()
    service.start()
    with service.client() as client:
        refused = client.patch("/v1/settings", json={"storm.max_conv_turn": 5})
        assert refused.status_code == 422
        assert refused.json() == {"message": "unknown setting retired.option"}

        assert client.delete("/v1/settings/retired.option").status_code == 200
        assert client.patch("/v1/settings", json={"storm.max_conv_turn": 5}).status_code == 200
    assert not (service.data / "kvasir.db").read_bytes().startswith(b"SQLite format 3")


# -- Methods ---------------------------------------------------------------------------------------


def test_methods_describe_their_options(client: httpx.Client) -> None:
    methods = client.get("/v1/methods").json()

    assert [(m["name"], m["clarifies"]) for m in methods] == [
        ("ledger", True),
        ("graph", True),
        ("storm", False),
    ]
    matches_shape("methods", methods)
    defaults = {m["name"]: m["options"]["properties"] for m in methods}
    assert defaults["storm"]["max_conv_turn"]["default"] == 3
    assert defaults["ledger"]["max_searches"]["default"] == 16


# -- Providers -------------------------------------------------------------------------------------


def test_providers_say_only_whether_they_answer(client: httpx.Client) -> None:
    assert client.get("/v1/providers").json() == [
        {"name": "down", "reachable": False},
        {"name": "gateway", "reachable": True},
        {"name": "ollama", "reachable": True},
    ]


def test_models_are_listed_sorted(client: httpx.Client) -> None:
    models = client.get("/v1/providers/gateway/models").json()

    matches_shape("provider.models", models)
    assert [(model["id"], model["ref"]) for model in models] == [
        ("fast", "gateway/fast"),
        ("strong", "gateway/strong"),
    ]
    assert models[1]["context_length"] == 16000


def test_ollama_says_more_of_its_models(client: httpx.Client) -> None:
    models = client.get("/v1/providers/ollama/models").json()

    matches_shape("provider.ollama_models", models)
    assert {model["context_length"] for model in models} == {8192}
    assert models[0]["capabilities"] == ["completion"]


def test_models_of_a_provider_that_cannot_be_asked(client: httpx.Client) -> None:
    missing = client.get("/v1/providers/nope/models")
    assert missing.status_code == 404
    assert missing.json() == {"message": "no provider nope"}

    down = client.get("/v1/providers/down/models")
    assert down.status_code == 502
    assert down.json() == {"message": "provider down did not list its models"}


# -- One call --------------------------------------------------------------------------------------


def test_clarify_asks_or_does_not(client: httpx.Client) -> None:
    before = len(client.get("/v1/runs").json())

    asked = client.post("/v1/clarify", json={"question": "The stone"})
    clear = client.post("/v1/clarify", json={"question": "A clear question"})

    assert asked.json() == {"questions": ["Which stone do you mean?"]}
    assert clear.json() == {"questions": []}
    assert len(client.get("/v1/runs").json()) == before


def test_a_task_is_one_completion_with_no_run(client: httpx.Client) -> None:
    before = len(client.get("/v1/runs").json())

    response = client.post(
        "/v1/tasks", json={"messages": [{"role": "user", "content": "Title this chat"}]}
    )

    assert response.json() == {"content": "A title"}
    assert len(client.get("/v1/runs").json()) == before


def test_one_call_takes_no_slot(client: httpx.Client, service: Service, fakes: Fakes) -> None:
    run = {
        "method": "ledger",
        "input": {"question": "x"},
        "options": {"max_searches": 1, "results_per_query": 1, "max_attempts": 1},
    }
    with service.client() as other:
        started = threading.Event()

        def hold() -> None:
            with other.stream("POST", "/v1/runs", json=run) as response:
                for _ in response.iter_lines():
                    started.set()

        with fakes.holding_chat():
            thread = threading.Thread(target=hold)
            thread.start()
            assert fakes.held.wait(30)
            assert client.post("/v1/runs", json=run).status_code == 429
            fakes.hold.set()
            answered = client.post("/v1/clarify", json={"question": "x"})
        thread.join(60)

    assert answered.status_code == 200


@pytest.mark.parametrize(
    ("path", "payload"),
    [("/v1/clarify", {"question": ""}), ("/v1/clarify", {}), ("/v1/tasks", {})],
)
def test_a_mistaken_call_is_refused(
    client: httpx.Client, path: str, payload: dict[str, Any]
) -> None:
    response = client.post(path, json=payload)

    assert response.status_code == 422
    assert {"loc", "msg", "type"} <= set(response.json()["detail"][0])


def test_a_gateway_refusing_a_task_fails_at_once(client: httpx.Client, fakes: Fakes) -> None:
    fakes.chat_status = 400
    try:
        response = client.post("/v1/tasks", json={"messages": [{"role": "user", "content": "x"}]})
    finally:
        fakes.chat_status = None

    assert response.status_code == 500


def test_a_run_uses_the_chosen_provider_with_its_key(client: httpx.Client, fakes: Fakes) -> None:
    fakes.requests.clear()

    transcript(
        client,
        "/v1/runs",
        json={
            "method": "ledger",
            "input": {"question": "When was the stone catalogued?"},
            "options": {"max_searches": 1, "results_per_query": 1, "max_attempts": 1},
        },
    )

    chats = [body for path, body in fakes.requests if path.endswith("/chat/completions")]
    assert {path for path, _ in fakes.requests if path.endswith("/chat/completions")} == {
        "/gw/v1/chat/completions"
    }
    assert {body["model"] for body in chats} == {"fast", "strong"}
    assert any(path.endswith("/embeddings") for path, _ in fakes.requests)
    assert any(path == "/searx" for path, _ in fakes.requests)
