"""The source routes, with Wikidata and the lists off, so nothing leaves the process."""

import contextlib

import pytest
from fastapi.testclient import TestClient

from fakes import AUTH, ENVIRONMENT, STORED, article_run, store_settings, stub_method
from kvasir.api.app import app
from kvasir.config import Settings
from kvasir.research.sources import _Match
from kvasir.runners import build_sources
from kvasir.schemas.results import ArticleResult, Citation
from test_runs import parse


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    data = tmp_path / "data"
    monkeypatch.setenv("KVASIR_DATA_DIR", str(data))
    sources = {
        "sources.wikidata": False,
        "sources.lists": [],
        "sources.allow": ["intranet.example"],
        "sources.deny": ["farm.example"],
    }

    @contextlib.contextmanager
    def make(enabled=True):
        store_settings(data, STORED | sources | {"sources.enabled": enabled})
        with TestClient(app, headers=AUTH) as client:
            yield client

    return make


@pytest.fixture
def client(make_client):
    with make_client() as client:
        yield client


def test_a_host_is_described_by_its_kind_and_tier(client):
    found = client.get("/v1/sources/www.nist.gov").json()

    assert found == {
        "kind": "government",
        "tags": [],
        "tier": "high",
        "label": "",
        "qid": "",
        "origin": "rule",
    }
    assert client.get("/v1/sources/example.org").json()["tier"] == "unknown"


def test_the_config_origin_is_exposed(client):
    allowed = client.get("/v1/sources/intranet.example").json()
    denied = client.get("/v1/sources/sub.farm.example").json()

    assert (allowed["origin"], allowed["tier"]) == ("config", "high")
    assert (denied["origin"], denied["tier"]) == ("config", "avoid")


def test_a_mark_is_set_and_cleared(client):
    marked = client.put("/v1/sources/example.org/mark", json={"value": "news"})

    assert marked.status_code == 200
    assert (marked.json()["kind"], marked.json()["origin"]) == ("news", "mark")
    assert client.get("/v1/sources/example.org").json()["origin"] == "mark"

    assert client.delete("/v1/sources/example.org/mark").status_code == 204
    assert client.get("/v1/sources/example.org").json()["origin"] == ""


def test_allow_and_deny_marks_set_the_tier(client):
    assert (
        client.put("/v1/sources/a.example/mark", json={"value": "deny"}).json()["tier"] == "avoid"
    )
    assert (
        client.put("/v1/sources/a.example/mark", json={"value": "allow"}).json()["tier"] == "high"
    )


def test_a_host_the_config_sets_cannot_be_marked(client):
    for host in ("intranet.example", "sub.farm.example"):
        put = client.put(f"/v1/sources/{host}/mark", json={"value": "news"})
        assert put.status_code == 409
        assert "sources.allow" in put.json()["message"]
        assert client.delete(f"/v1/sources/{host}/mark").status_code == 409


@pytest.mark.parametrize("value", ["trusted", "unknown", ""])
def test_a_bad_value_is_refused(client, value):
    put = client.put("/v1/sources/example.org/mark", json={"value": value})

    assert put.status_code == 422
    assert client.get("/v1/sources/example.org").json()["origin"] == ""


def test_a_body_without_a_value_or_a_bad_host_is_refused(client):
    assert client.put("/v1/sources/example.org/mark", json={}).status_code == 422
    assert client.put("/v1/sources/localhost/mark", json={"value": "news"}).status_code == 422


def test_flushing_forgets_the_cache_and_keeps_the_marks(client):
    sources = app.state.sources
    sources._store.put("example.net", _Match("news", frozenset(), "", "Q1"))
    client.put("/v1/sources/example.org/mark", json={"value": "news"})

    assert client.delete("/v1/sources").status_code == 204

    assert sources._store.get("example.net", 30, 7) is None
    assert client.get("/v1/sources/example.org").json()["origin"] == "mark"


@pytest.mark.parametrize(
    ("marked", "asked"),
    [
        ("example.org.", "example.org"),
        ("EXAMPLE.org", "example.org"),
        ("www.example.org", "example.org"),
        ("example.org", "WWW.Example.org."),
        ("bücher.example", "xn--bcher-kva.example"),
    ],
)
def test_every_spelling_of_a_host_reaches_the_same_mark(client, marked, asked):
    assert client.put(f"/v1/sources/{marked}/mark", json={"value": "news"}).status_code == 200

    assert client.get(f"/v1/sources/{asked}").json()["origin"] == "mark"
    assert client.delete(f"/v1/sources/{asked}/mark").status_code == 204
    assert client.get(f"/v1/sources/{marked}").json()["origin"] == ""


def test_a_host_that_is_no_dns_name_is_refused(client):
    assert client.get("/v1/sources/localhost").status_code == 422


def test_a_mark_that_outlived_a_config_entry_can_still_be_cleared(client):
    sources = app.state.sources
    sources._store.mark("farm.example", "news")

    assert client.delete("/v1/sources/farm.example/mark").status_code == 204
    assert client.delete("/v1/sources/farm.example/mark").status_code == 409
    assert sources.marked("farm.example") is False


def test_a_runs_citations_show_the_marks_of_now(client, monkeypatch):
    def run(settings, input, options, stream):
        citation = Citation(index=1, url="https://www.example.org/a", title="", snippet="")
        return ArticleResult(article="a", outline="o", citations=[citation], duration_seconds=1.0)

    stub_method(monkeypatch, "storm", run)
    events = parse(client.post("/v1/runs", json=article_run("The Kvasir stone")).text)
    run_id = events[0][1]["run_id"]
    done = dict(events)["done"]["citations"][0]["source"]

    def kind():
        return client.get(f"/v1/runs/{run_id}").json()["result"]["citations"][0]["source"]["kind"]

    assert done["kind"] == kind() == "unknown"
    client.put("/v1/sources/example.org/mark", json={"value": "news"})
    assert kind() == "news"


def test_a_mark_reaches_the_instance_runs_use(client):
    client.put("/v1/sources/example.org/mark", json={"value": "deny"})

    runs = build_sources(Settings.load())
    assert runs is app.state.sources
    assert runs.classify(["example.org"])["example.org"].tier == "avoid"


def test_the_routes_are_absent_when_sources_are_off(make_client):
    with make_client(enabled=False) as client:
        assert client.get("/v1/sources/example.org").status_code == 404
        assert client.delete("/v1/sources").status_code == 404
        assert client.put("/v1/sources/example.org/mark", json={"value": "news"}).status_code == 404
        assert client.delete("/v1/sources/example.org/mark").status_code == 404
