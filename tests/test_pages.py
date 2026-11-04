"""The page cache across runs: fresh, stale and revalidated reads, and what a failure leaves."""

from __future__ import annotations

import contextlib
import contextvars
import sqlite3
import threading

import httpx
import numpy as np
import pytest
from dspy.utils.dummies import DummyLM

from fakes import Stream, Vectors
from kvasir.methods.ledger.loop import research
from kvasir.methods.ledger.questions import Question
from kvasir.research import fetch
from kvasir.research.claims import Claim
from kvasir.research.corpus import Corpus
from kvasir.research.fetch import FetchError, Link, Page
from kvasir.research.pages import PageStore

URL = "https://example.org/a"
HOUR = 3600


def article(word, href="/next"):
    body = f"<p>{word} is what this page says, at some length, so that it reads as an article.</p>"
    return f'<html><body><article>{body}<p><a href="{href}">more</a></p></article></body></html>'


@pytest.fixture
def web(monkeypatch, tmp_path):
    """A stubbed site: `requests` records each request's headers, `reply` the next answer."""

    class Web:
        requests: list[dict[str, str]] = []
        status = 200
        body = article("first")
        headers = {"etag": '"v1"', "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT"}

    def stream(method, url, **kwargs):
        Web.requests.append(kwargs["headers"])
        response = httpx.Response(
            Web.status,
            headers={"content-type": "text/html", **Web.headers},
            content=Web.body,
            request=httpx.Request(method, url),
        )
        return contextlib.nullcontext(response)

    Web.requests = []
    monkeypatch.setattr(fetch.httpx, "stream", stream)
    return Web


@pytest.fixture
def store(tmp_path):
    store = PageStore(tmp_path / "pages.db")
    fetch.configure_cache(store, 48)
    return store


def age(store, hours):
    with store._connect() as db:
        db.execute("UPDATE pages SET fetched_at = fetched_at - ?", (hours * HOUR,))


def test_a_second_fetch_makes_no_request(web, store):
    first = fetch.fetch_page(URL)
    second = fetch.fetch_page(URL)

    assert len(web.requests) == 1
    assert second == first


def test_a_stale_copy_is_asked_for_again_and_a_304_refreshes_it(web, store):
    first = fetch.fetch_page(URL)
    age(store, 49)
    web.status, web.body = 304, b""

    second = fetch.fetch_page(URL)

    sent = web.requests[1]
    assert (sent["If-None-Match"], sent["If-Modified-Since"]) == (
        '"v1"',
        "Mon, 01 Jan 2024 00:00:00 GMT",
    )
    assert second.text == first.text
    assert second.fetched_at > first.fetched_at
    web.requests.clear()
    assert fetch.fetch_page(URL).fetched_at == second.fetched_at
    assert web.requests == []


def test_a_200_on_revalidation_replaces_the_text_and_the_links(web, store):
    fetch.fetch_page(URL)
    age(store, 49)
    web.body, web.headers = article("second", "/other"), {"etag": '"v2"'}

    page = fetch.fetch_page(URL)

    assert "second" in page.text
    assert [link.url for link in page.links] == ["https://example.org/other"]
    cached, etag, last_modified = store.get(URL)
    assert ("second" in cached.text, etag, last_modified) == (True, '"v2"', None)
    assert [link.url for link in cached.links] == ["https://example.org/other"]


def test_a_max_age_of_zero_fetches_again_without_conditions(web, store):
    fetch.configure_cache(store, 0)
    fetch.fetch_page(URL)
    web.body = article("second")

    page = fetch.fetch_page(URL)

    assert len(web.requests) == 2
    assert "If-None-Match" not in web.requests[1]
    assert "second" in page.text
    assert "second" in store.get(URL)[0].text


def test_a_failure_stores_nothing_and_keeps_the_old_copy(web, store):
    fetch.fetch_page(URL)
    age(store, 49)
    web.status = 500

    with pytest.raises(FetchError):
        fetch.fetch_page(URL)
    with pytest.raises(FetchError):
        fetch.fetch_page("https://example.org/missing")

    assert "first" in store.get(URL)[0].text
    assert store.get("https://example.org/missing") is None


def test_spellings_of_one_url_share_a_row(web, store):
    fetch.fetch_page("https://www.example.org/a/")
    page = fetch.fetch_page("http://example.org/a")

    assert len(web.requests) == 1
    assert page.url == "http://example.org/a"
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 1


def test_links_round_trip_with_their_regions(store):
    links = [
        Link("https://b.example/x", "x", "see x here", "main"),
        Link("https://b.example/y", "y", "", "footer"),
        Link("https://b.example/z", "z", "also z", "related"),
    ]
    page = fetch.Page(URL, "https://example.org/final", "T", "body", "render", links, 12.5)

    store.put(page, None, None)

    assert store.get(URL) == (page, None, None)


def test_concurrent_puts_do_not_error(store):
    errors = []

    def put(n):
        try:
            for i in range(10):
                page = fetch.Page(
                    f"https://example.org/{n}",
                    "f",
                    "",
                    f"text {i}",
                    "static",
                    [Link("https://b.example", "a", "c", "main")],
                    float(i),
                )
                store.put(page, None, None)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=put, args=(n % 3,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert store.get("https://example.org/0")[0].text == "text 9"


def test_a_failing_cache_costs_the_download_and_not_the_run(web, store, monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "get", broken)
    monkeypatch.setattr(store, "put", broken)

    assert "first" in fetch.fetch_page(URL).text
    assert len(web.requests) == 1


def kept(store, url, texts, model="m", vector=(1.0, 0.0)):
    """A page of `url` cached, as a run's corpus keeps it, its pieces all of one vector."""
    cached = Corpus(store, model)
    cached.add(
        page(url, " ".join(texts)),
        texts,
        np.array([vector] * len(texts)),
    )
    return cached


def page(url, text="x"):
    return Page(url, url, "", text, "static", [], 1.0)


def test_a_later_corpus_recalls_a_piece_an_earlier_one_kept(store):
    store.put(page("https://a.org/x"), None, None)
    kept(store, "https://a.org/x", ["the stone was found in 1904"])

    recalled = Corpus(store, "m").recall(Vectors({"stone found": [1.0, 0.0]}), "stone found")

    assert [(p.url, text) for p, text in recalled] == [
        ("https://a.org/x", "the stone was found in 1904")
    ]


def test_a_piece_of_another_embedding_model_is_ignored(store):
    store.put(page("https://a.org/x"), None, None)
    kept(store, "https://a.org/x", ["the stone was found in 1904"], model="old")

    assert Corpus(store, "m").recall(Vectors({"stone": [1.0, 0.0]}), "stone") == []


def test_a_page_in_the_corpus_is_not_drawn_from_the_store_again(store):
    store.put(page("https://a.org/x"), None, None)
    corpus = kept(store, "https://a.org/x", ["the stone was found"])
    corpus.pieces[0].read = True

    assert corpus.recall(Vectors({"stone": [1.0, 0.0]}), "stone") == []


def test_a_recalled_cached_page_joins_the_corpus_and_is_not_recalled_twice(store):
    store.put(page("https://a.org/x"), None, None)
    kept(store, "https://a.org/x", ["the stone one", "the stone two"])
    corpus = Corpus(store, "m")
    encoder = Vectors({"stone": [1.0, 0.0]})

    assert len(corpus.recall(encoder, "stone")) == 2
    assert corpus.recall(encoder, "stone") == []
    assert corpus.pages == {"a.org": 1}
    assert all(piece.read for piece in corpus.pieces)


def test_replacing_a_page_drops_its_old_pieces(store):
    store.put(page("https://a.org/x"), None, None)
    kept(store, "https://a.org/x", ["the stone was found"])
    store.put(page("https://a.org/x", "new text"), None, None)

    assert store.search("stone", "m", []) == []


def test_a_store_error_while_adding_is_logged_and_the_corpus_keeps_the_pieces(store, caplog):
    store._path = store._path.parent / "missing" / "pages.db"
    corpus = Corpus(store, "m")

    added = corpus.add(page("https://a.org/x"), ["piece"], np.array([[1.0, 0.0]]))

    assert corpus.pieces == added
    assert "not cached" in caplog.text


def test_a_question_without_words_finds_nothing(store):
    store.put(page("https://a.org/x"), None, None)
    kept(store, "https://a.org/x", ["the stone was found"])

    assert store.search("?! -- ()", "m", []) == []
    assert store.search('"', "m", []) == []


def test_a_page_recalled_in_part_can_give_its_other_pieces_to_a_later_question(store):
    store.put(page("https://a.org/x"), None, None)
    kept(store, "https://a.org/x", ["the stone one", "the stone two", "the stone three"])
    corpus = Corpus(store, "m")
    encoder = Vectors({"stone": [1.0, 0.0]})

    first = corpus.recall(encoder, "stone")
    second = corpus.recall(encoder, "stone")

    assert len(first) == 2
    assert len(second) == 1
    assert {text for _, text in first + second} == {
        "the stone one",
        "the stone two",
        "the stone three",
    }
    assert corpus.pages == {"a.org": 1}


def test_a_second_run_answers_from_the_page_of_the_first_with_no_search_and_no_fetch(
    store, monkeypatch
):
    def fail(*args):
        raise AssertionError("no fetch expected")

    monkeypatch.setattr("kvasir.research.read.fetch_page", fail)
    store.put(page("https://a.org/x"), None, None)
    kept(store, "https://a.org/x", ["the stone was found in 1904"])
    model = DummyLM(
        [{"findings": [{"claim": "Found", "quote": "found in 1904", "confidence": "high"}]}]
    )
    question = Question(id=1, text="stone")
    claims: list[Claim] = []

    def judge(judged):
        judged.status = "answered"
        judged.judged_at = len(claims)

    searches = research(
        {"plan": None, "intake": model},
        Vectors({"stone": [1.0, 0.0]}),
        fail,
        "2026-11-01",
        "brief",
        [question],
        claims,
        set(),
        Corpus(store, "m"),
        4,
        1,
        3,
        judge,
        0,
        Stream(),
    )

    assert searches == 0
    assert [(c.id, c.url, c.quote) for c in claims] == [(1, "https://a.org/x", "found in 1904")]


def test_without_recall_of_past_runs_a_stored_piece_is_ignored_but_pieces_are_kept(store):
    store.put(page("https://a.org/x"), None, None)
    kept(store, "https://a.org/x", ["the stone was found in 1904"])
    encoder = Vectors({"stone": [1.0, 0.0]})

    assert Corpus(store, "m", recall_past_runs=False).recall(encoder, "stone") == []
    store.put(page("https://b.org/y", "the gold"), None, None)
    own = Corpus(store, "m", recall_past_runs=False)
    own.add(page("https://b.org/y", "the gold"), ["the gold was lost"], np.array([[0.0, 1.0]]))

    assert own.recall(Vectors({"gold": [0.0, 1.0]}), "gold") != []
    later = Corpus(store, "m").recall(Vectors({"gold": [0.0, 1.0]}), "gold")
    assert [text for _, text in later] == ["the gold was lost"]


def test_a_research_loop_without_recall_of_past_runs_makes_its_search(store):
    store.put(page("https://a.org/x"), None, None)
    kept(store, "https://a.org/x", ["the stone was found in 1904"])
    searched = []

    def search(query, exclude_urls):
        searched.append(query)
        return []

    search.failures = []
    question = Question(id=1, text="stone")

    searches = research(
        {"plan": DummyLM([{"query": "q"}]), "intake": None},
        Vectors({"stone": [1.0, 0.0]}),
        search,
        "2026-11-01",
        "brief",
        [question],
        [],
        set(),
        Corpus(store, "m", recall_past_runs=False),
        1,
        1,
        3,
        lambda judged: None,
        0,
        Stream(),
    )

    assert (searches, searched) == (1, ["q"])


def test_a_max_age_set_for_a_run_fetches_again_and_replaces_the_copy(web, store):
    fetch.fetch_page(URL)
    kept(store, URL, ["first"])
    web.body = article("second")

    def in_a_run():
        fetch.use_max_age(0)
        return fetch.fetch_page(URL)

    page = contextvars.copy_context().run(in_a_run)

    assert len(web.requests) == 2
    assert "second" in page.text
    cached = store.get(URL)[0]
    assert "second" in cached.text
    # The pieces belonged to the old text.
    assert store.search("first", "m", []) == []


def test_a_max_age_set_in_one_context_does_not_reach_another(web, store):
    contextvars.copy_context().run(fetch.use_max_age, 0)

    fetch.fetch_page(URL)
    fetch.fetch_page(URL)

    assert len(web.requests) == 1
