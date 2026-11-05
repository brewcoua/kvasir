"""The page cache routes, over a store seeded in a temporary directory."""

import contextlib
import time

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from fakes import AUTH, Vectors
from kvasir.api import pages
from kvasir.api.app import app
from kvasir.config import ConfigError
from kvasir.research import fetch
from kvasir.research.corpus import RECALL_MIN
from kvasir.research.fetch import Link, Page, normalize
from kvasir.research.pages import page_id
from kvasir.schemas.results import ArticleResult, Citation
from test_main import ENVIRONMENT

FRESH = "Fresh is what this page says, at some length, so that it reads as an article.\nmore"
A = "https://example.org/a"
B = "https://example.org/b"
C = "https://other.net/c"
NOW = time.time()


def page(url, title, fetched_at, engine="static", links=()):
    return Page(url, url, title, f"# {title}\n\nBody of {title}.", engine, list(links), fetched_at)


@pytest.fixture
def client(monkeypatch, tmp_path):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app, headers=AUTH) as client:
        yield client


@pytest.fixture
def store(client):
    store = fetch.cache()
    links = [
        Link("https://example.org/x", "an article", "ctx", "related"),
        Link("https://example.org/y", "home", "", "nav"),
    ]
    store.put(page(A, "Alpha", NOW - 300, links=links), None, None)
    store.put(page(B, "Beta", NOW - 200, "render"), None, None)
    store.put(page(C, "Gamma", NOW - 100, "stealth"), None, None)
    store.put_pieces(A, ["alpha one", "alpha two"], np.eye(2, 3), "fake")
    store.put_pieces(C, ["gamma one"], np.array([[0.0, 0.0, 1.0]]), "fake")
    return store


def test_sites_are_listed_by_last_fetch(client, store):
    sites = client.get("/v1/sites").json()

    assert [site["domain"] for site in sites] == ["other.net", "example.org"]
    assert sites[1]["pages"] == 2
    assert sites[1]["pieces"] == 2
    assert sites[1]["engines"] == ["render", "static"]
    assert sites[1]["fetched_at"] == pytest.approx(NOW - 200)


def test_pages_are_listed_newest_first_and_by_site(client, store, monkeypatch):
    everything = client.get("/v1/pages").json()
    assert [row["title"] for row in everything] == ["Gamma", "Beta", "Alpha"]

    mine = client.get("/v1/pages", params={"site": "example.org"}).json()
    assert [row["title"] for row in mine] == ["Beta", "Alpha"]
    assert mine[1] == {
        "id": page_id(A),
        "url": A,
        "title": "Alpha",
        "engine": "static",
        "fetched_at": pytest.approx(NOW - 300),
        "pieces": 2,
        "length": len("# Alpha\n\nBody of Alpha."),
    }

    assert client.get("/v1/pages", params={"site": "nowhere.org"}).json() == []

    monkeypatch.setattr(pages, "PAGE_LIMIT", 1)
    assert len(client.get("/v1/pages").json()) == 1
    assert len(client.get("/v1/pages", params={"site": "example.org"}).json()) == 2


def test_a_page_has_its_text_leads_by_region_and_citing_runs(client, store):
    run = client.app.state.runs.start("research", "what does alpha say?")
    run.set_result(
        ArticleResult(
            article="x",
            outline="x",
            citations=[Citation(index=1, url=A, title="A", snippet="s")],
            duration_seconds=1.0,
        )
    )
    client.app.state.runs.start("research", "unrelated")

    body = client.get(f"/v1/pages/{page_id(A)}").json()

    assert body["id"] == page_id(A)
    assert body["text"] == "# Alpha\n\nBody of Alpha."
    assert body["final_url"] == A
    assert body["leads"] == {
        "related": [{"url": "https://example.org/x", "anchor": "an article", "context": "ctx"}],
        "nav": [{"url": "https://example.org/y", "anchor": "home", "context": ""}],
    }
    assert body["runs"] == [{"id": run.id, "topic": "what does alpha say?"}]


def test_an_unknown_page_is_not_found(client, store):
    assert client.get("/v1/pages/0123456789abcdef").status_code == 404
    assert client.delete("/v1/pages/0123456789abcdef").status_code == 404
    assert client.post("/v1/pages/0123456789abcdef/refetch").status_code == 404
    assert client.delete("/v1/sites/nowhere.org").status_code == 404


def test_recall_scores_pieces_without_marking_them_read(client, store, monkeypatch):
    encoder = Vectors({"alpha": [1.0, 0.0, 0.0]}, default=[0.0, 0.0, 1.0])
    monkeypatch.setattr(pages, "build_research_tools", lambda settings: (encoder, None))

    body = client.get("/v1/pages/recall", params={"q": "alpha"}).json()

    assert body["minimum"] == RECALL_MIN
    assert [(p["text"], p["similarity"], p["clears"], p["site"]) for p in body["pieces"]] == [
        ("alpha one", pytest.approx(1.0), True, "example.org"),
        ("alpha two", pytest.approx(0.0), False, "example.org"),
    ]
    assert body["pieces"][0]["page"] == page_id(A)

    encoder.vectors["alpha"] = [1.0, 1.0, 1.0]
    low = client.get("/v1/pages/recall", params={"q": "alpha"}).json()["pieces"]
    assert [p["clears"] for p in low] == [False, False]
    assert low[0]["similarity"] == pytest.approx(3**-0.5)


def test_recall_is_unavailable_without_an_embedding_model(client, store, monkeypatch):
    def refuse(settings):
        raise ConfigError("model 'embed' names unknown provider 'nowhere'")

    monkeypatch.setattr(pages, "build_research_tools", refuse)
    response = client.get("/v1/pages/recall", params={"q": "alpha"})

    assert response.status_code == 503
    assert "nowhere" in response.json()["message"]


def search(store, words):
    return [text for _, text, _ in store.search(words, "fake", [])]


def test_deleting_a_page_drops_its_links_pieces_and_search_hits(client, store):
    assert search(store, "alpha") == ["alpha one", "alpha two"]

    assert client.delete(f"/v1/pages/{page_id(A)}").status_code == 204

    assert client.get(f"/v1/pages/{page_id(A)}").status_code == 404
    assert search(store, "alpha") == []
    assert search(store, "gamma") == ["gamma one"]
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM links").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM pieces WHERE page = ?", (A,)).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM pages").fetchone() == (2,)


def test_deleting_a_site_drops_all_its_pages(client, store):
    assert client.delete("/v1/sites/example.org").status_code == 204

    assert [site["domain"] for site in client.get("/v1/sites").json()] == ["other.net"]
    assert search(store, "alpha") == []
    assert search(store, "gamma") == ["gamma one"]


def test_stats_count_what_the_cache_holds(client, store):
    body = client.get("/v1/pages/stats").json()

    texts = [f"# {t}\n\nBody of {t}." for t in ("Alpha", "Beta", "Gamma")]
    assert body == {
        "pages": 3,
        "links": 2,
        "pieces": 3,
        "text_bytes": sum(len(text) for text in texts),
        "vector_bytes": 3 * 3 * 4,
        "file_bytes": body["file_bytes"],
        "path": str(store._path),
        "oldest": pytest.approx(NOW - 300),
        "newest": pytest.approx(NOW - 100),
    }
    assert body["file_bytes"] >= store._path.stat().st_size > 0


def test_flushing_empties_the_cache_and_leaves_it_writable(client, store):
    assert client.delete("/v1/pages").status_code == 204

    assert client.get("/v1/pages").json() == []
    assert search(store, "alpha") == []
    assert search(store, "gamma") == []
    stats = client.get("/v1/pages/stats").json()
    assert (stats["pages"], stats["links"], stats["pieces"]) == (0, 0, 0)
    assert (stats["oldest"], stats["newest"]) == (None, None)
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pieces_fts").fetchone() == (0,)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    store.put(page(A, "Alpha", NOW), None, None)
    assert client.get("/v1/pages").json()[0]["title"] == "Alpha"


def test_the_site_graph_counts_links_between_cached_sites(client, store):
    store.put(
        page(
            C,
            "Gamma",
            NOW,
            links=[
                Link("https://www.example.org/a", "", "", "main"),
                Link("https://example.org/b", "", "", "main"),
                Link("https://other.net/d", "", "", "nav"),
                Link("https://nowhere.io/", "", "", "main"),
            ],
        ),
        None,
        None,
    )

    body = client.get("/v1/sites/graph").json()

    assert {(n["domain"], n["pages"], n["links"]) for n in body["sites"]} == {
        ("example.org", 2, 2),
        ("other.net", 1, 4),
    }
    # The two links of example.org lead to its own pages, and nowhere.io is not cached.
    assert body["edges"] == [{"source": "other.net", "target": "example.org", "count": 2}]


def test_every_route_is_unavailable_without_a_cache(client):
    fetch.configure_cache(None, 0)
    id = page_id(A)

    for method, path in [
        ("get", "/v1/sites"),
        ("get", "/v1/sites/graph"),
        ("get", "/v1/pages"),
        ("get", "/v1/pages/stats"),
        ("delete", "/v1/pages"),
        ("get", "/v1/pages/recall?q=x"),
        ("get", f"/v1/pages/{id}"),
        ("delete", f"/v1/pages/{id}"),
        ("delete", "/v1/sites/example.org"),
        ("post", f"/v1/pages/{id}/refetch"),
    ]:
        response = client.request(method, path)
        assert response.status_code == 503, path
        assert "cache" in response.json()["message"]


@pytest.fixture
def web(monkeypatch):
    class Web:
        requests: list[str] = []
        status = 200
        body = (
            "<html><body><article><p>Fresh is what this page says, at some length, so that it "
            'reads as an article.</p><p><a href="/next">more</a></p></article></body></html>'
        )

    def stream(method, url, **kwargs):
        Web.requests.append(url)
        response = httpx.Response(
            Web.status,
            headers={"content-type": "text/html"},
            content=Web.body,
            request=httpx.Request(method, url),
        )
        return contextlib.nullcontext(response)

    Web.requests = []
    monkeypatch.setattr(fetch.httpx, "stream", stream)
    return Web


def test_refetching_replaces_the_page_and_its_pieces_whatever_its_age(client, store, web):
    # The page is fresh, so a plain fetch makes no request: only the refetch does.
    assert fetch.fetch_page(A).text == "# Alpha\n\nBody of Alpha."
    before = len(web.requests)

    response = client.post(f"/v1/pages/{page_id(A)}/refetch")

    assert response.status_code == 200, response.text
    assert len(web.requests) == before + 1
    body = response.json()
    assert body["id"] == page_id(normalize(A))
    assert body["text"] == FRESH
    assert search(store, "alpha") == []


def test_a_refetch_that_fails_is_a_bad_gateway(client, store, web):
    web.status = 404

    response = client.post(f"/v1/pages/{page_id(A)}/refetch")

    assert response.status_code == 502
    assert response.json()["message"]
    assert client.get(f"/v1/pages/{page_id(A)}").json()["text"] == "# Alpha\n\nBody of Alpha."


def test_a_refetch_does_not_leave_the_cache_off_for_the_next_request(
    client, store, web, monkeypatch
):
    seen = []
    real = fetch.fetch_page
    monkeypatch.setattr(
        fetch, "fetch_page", lambda url: (seen.append(fetch._max_age.get()), real(url))[1]
    )

    client.post(f"/v1/pages/{page_id(A)}/refetch")
    client.get(f"/v1/pages/{page_id(A)}")

    # Set for the refetch alone: a route runs in a copy of the caller's context, on a thread.
    assert seen == [0]
    assert fetch._max_age.get() is None
    requests = len(web.requests)
    real(A)
    assert len(web.requests) == requests


def test_sites_carry_no_source_when_sources_are_off(client, store):
    assert all(site["source"] is None for site in client.get("/v1/sites").json())
    assert all(site["source"] is None for site in client.get("/v1/sites/graph").json()["sites"])


def test_sites_and_graph_nodes_carry_their_source_when_sources_are_on(
    client, store, monkeypatch, tmp_path
):
    monkeypatch.setenv("KVASIR_SOURCES_ENABLED", "true")
    monkeypatch.setenv("KVASIR_SOURCES_WIKIDATA", "false")
    monkeypatch.setenv("KVASIR_SOURCES_LISTS", "")
    monkeypatch.setenv("KVASIR_SOURCES_DENY", "other.net")
    with TestClient(app, headers=AUTH) as sourced:
        fetch.cache().put(page("https://stats.gov/a", "Stats", NOW), None, None)
        listed = {site["domain"]: site["source"] for site in sourced.get("/v1/sites").json()}
        nodes = {s["domain"]: s["source"] for s in sourced.get("/v1/sites/graph").json()["sites"]}

    for found in (listed, nodes):
        assert (found["stats.gov"]["kind"], found["stats.gov"]["tier"]) == ("government", "high")
        assert (found["other.net"]["tier"], found["other.net"]["origin"]) == ("avoid", "config")
        assert found["example.org"]["tier"] == "unknown"
