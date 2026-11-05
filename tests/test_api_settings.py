"""Reading and changing the stored settings through the API, and the change applying at once."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from fakes import AUTH, ENVIRONMENT, STORED, store_settings
from kvasir.api import settings as settings_api
from kvasir.api.app import app
from kvasir.research import fetch
from kvasir.settings_store import SettingsStore
from kvasir.storm import runtime


@pytest.fixture
def data(monkeypatch, tmp_path):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    data = tmp_path / "data"
    monkeypatch.setenv("KVASIR_DATA_DIR", str(data))
    store_settings(data, STORED)
    return data


@pytest.fixture
def client(data):
    with TestClient(app, headers=AUTH) as client:
        yield client


def field(view, key):
    return next(f for f in view["fields"] if f["key"] == key)


def test_every_stored_setting_is_listed_with_what_it_accepts(client):
    view = client.get("/v1/settings").json()

    assert field(view, "model.fast") == {
        "key": "model.fast",
        "type": "model",
        "help": "Conversation simulation, question asking, polishing, and research intake.",
        "value": STORED["model.fast"],
        "default": "",
        "stored": True,
        "required": True,
        "nullable": False,
        "restart": False,
        "minimum": None,
        "choices": None,
    }
    assert field(view, "research.max_searches")["value"] == 16
    assert field(view, "research.max_searches")["stored"] is False
    assert field(view, "research.max_searches")["minimum"] == 1
    assert field(view, "fetch.max_age_hours")["minimum"] == 0
    assert field(view, "runs.store")["choices"] == ["memory", "sqlite"]
    assert field(view, "runs.store")["restart"] is True
    assert field(view, "sources.lists")["type"] == "list"
    assert field(view, "model.reason_effort")["nullable"] is True
    assert all(f["help"] for f in view["fields"])
    assert view["missing"] == []
    assert view["restart_required"] == []


def test_a_change_is_stored_and_applies_at_once(client, data):
    response = client.patch(
        "/v1/settings", json={"research.max_searches": 9, "model.context_tokens": 8192}
    )

    assert response.status_code == 200
    changed = field(response.json(), "research.max_searches")
    assert (changed["value"], changed["stored"]) == (9, True)
    assert app.state.settings.research.max_searches == 9
    assert runtime.context_tokens() == 8192
    assert SettingsStore(data / "kvasir.db").load()["research.max_searches"] == "9"


def test_null_and_delete_give_back_the_default(client, data):
    client.patch("/v1/settings", json={"research.max_searches": 9, "storm.max_conv_turn": 5})

    client.patch("/v1/settings", json={"research.max_searches": None})
    response = client.delete("/v1/settings/storm.max_conv_turn")

    assert response.status_code == 200
    assert app.state.settings.research.max_searches == 16
    assert app.state.settings.storm.max_conv_turn == 3
    assert "storm.max_conv_turn" not in SettingsStore(data / "kvasir.db").load()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"research.max_searches": 0}, "research.max_searches must be at least 1"),
        ({"research.max_searches": "9"}, "research.max_searches must be an integer"),
        ({"runs.store": "postgres"}, "runs.store must be memory or sqlite"),
        ({"model.fast": "elsewhere/m"}, "unknown provider 'elsewhere'"),
        ({"research.nothing": 1}, "unknown setting research.nothing"),
        # Checked against the other stored settings, not alone.
        ({"favicons.providers": ["custom"]}, "favicons.custom_url"),
    ],
)
def test_a_bad_change_writes_nothing(client, data, changes, message):
    before = SettingsStore(data / "kvasir.db").load()

    response = client.patch("/v1/settings", json={"research.max_depth": 3} | changes)

    assert response.status_code == 422
    assert message in response.json()["message"]
    assert SettingsStore(data / "kvasir.db").load() == before
    assert app.state.settings.research.max_depth == 2


def test_a_key_this_version_does_not_know_can_be_deleted(data):
    SettingsStore(data / "kvasir.db").write({"old.setting": 1})
    with TestClient(app, headers=AUTH) as client:
        assert client.patch("/v1/settings", json={"storm.max_conv_turn": 5}).status_code == 422
        assert client.delete("/v1/settings/old.setting").status_code == 200
        assert client.patch("/v1/settings", json={"storm.max_conv_turn": 5}).status_code == 200


def test_setting_what_is_missing_ends_setup(data, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    monkeypatch.setenv("KVASIR_DATA_DIR", str(empty))
    with TestClient(app, headers=AUTH) as client:
        assert client.get("/v1/settings").json()["missing"] == list(STORED)
        assert client.get("/readyz").status_code == 503

        response = client.patch("/v1/settings", json=STORED)

        assert response.json()["missing"] == []
        assert app.state.settings.model.fast == STORED["model.fast"]


def test_a_setting_read_at_startup_waits_for_a_restart(client, data):
    response = client.patch("/v1/settings", json={"runs.store": "sqlite"})

    assert response.json()["restart_required"] == ["runs.store"]
    assert field(response.json(), "runs.store")["value"] == "sqlite"
    with TestClient(app, headers=AUTH) as restarted:
        assert restarted.get("/v1/settings").json()["restart_required"] == []


def test_the_run_limit_changes_while_runs_hold_slots(client):
    slots = app.state.run_slots
    assert slots.acquire()
    assert not slots.acquire()

    client.patch("/v1/settings", json={"runs.max_concurrent": 2})
    assert slots.acquire()
    client.patch("/v1/settings", json={"runs.max_concurrent": 1})
    slots.release()
    # Still one held, at a limit of one.
    assert not slots.acquire()
    slots.release()
    assert slots.acquire()
    slots.release()


def test_favicons_and_fetch_are_rebuilt_on_a_change(client):
    favicons = app.state.favicons
    pages = fetch.cache()

    client.patch("/v1/settings", json={"research.max_depth": 3})
    assert app.state.favicons is favicons

    client.patch("/v1/settings", json={"favicons.providers": [], "fetch.max_age_hours": 0})
    assert app.state.favicons is None
    assert fetch.cache() is pages
    assert fetch._cache == (pages, 0)


def test_the_same_page_store_survives_a_fetch_change(client):
    pages = fetch.cache()
    client.patch("/v1/settings", json={"fetch.contact": "me@example.org"})
    assert fetch.cache() is pages
    assert runtime._fetch_contact == "me@example.org"


@pytest.mark.parametrize("body", [[1], "x", None])
def test_a_body_that_is_not_an_object_is_refused(client, body):
    response = client.patch("/v1/settings", json=body)

    assert response.status_code == 422
    assert response.json() == {"message": "send an object of values by key"}


def test_a_value_is_stored_as_it_is_read(client, data):
    client.patch(
        "/v1/settings",
        json={
            "search.url": " http://searxng.example/ ",
            "fetch.render": "OFF",
            "fetch.host_interval_seconds": 2,
            "model.reason_effort": "",
        },
    )

    rows = SettingsStore(data / "kvasir.db").load()
    assert rows["search.url"] == '"http://searxng.example"'
    assert rows["fetch.render"] == '"off"'
    assert rows["fetch.host_interval_seconds"] == "2.0"
    # Off, rather than back to its default of medium.
    assert rows["model.reason_effort"] == '""'
    assert app.state.settings.model.reason_effort is None


def test_a_fetch_change_keeps_what_each_host_asked_for(client):
    runtime.delay_host("a.example", 30.0)
    waiting = runtime._hosts["a.example"]

    client.patch("/v1/settings", json={"fetch.contact": "me@example.org"})

    assert runtime._hosts["a.example"] is waiting


def test_a_change_that_cannot_be_written_is_undone(client, monkeypatch):
    def locked(self, changes):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SettingsStore, "write", locked)

    with pytest.raises(sqlite3.OperationalError):
        client.patch("/v1/settings", json={"research.max_depth": 3, "runs.max_concurrent": 4})

    assert app.state.settings.research.max_depth == 2
    assert app.state.run_slots.limit == 1


def test_a_change_whose_build_fails_writes_and_applies_nothing(client, data, monkeypatch):
    def broken(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(settings_api.FaviconStore, "__init__", broken)
    before = SettingsStore(data / "kvasir.db").load()

    with pytest.raises(OSError):
        client.patch("/v1/settings", json={"favicons.ttl_days": 9, "research.max_depth": 3})

    assert SettingsStore(data / "kvasir.db").load() == before
    assert app.state.settings.research.max_depth == 2
