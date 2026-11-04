"""Leads: the links of pages read, followed when they look worth more than a search result."""

from __future__ import annotations

import httpx
import numpy as np
import pytest

from fakes import Search, Stream, Vectors, claim
from kvasir.research import fetch
from kvasir.research import read as reading
from kvasir.research.claims import Claim
from kvasir.research.corpus import (
    LEAD_DEPTH,
    PAGES_PER_SITE,
    SITE_AFFINITY,
    SITE_SEARCHES,
    Corpus,
)
from kvasir.research.fetch import FetchError, Link, Page, allowed
from kvasir.research.read import read
from kvasir.research.search import search

QUESTION = "when was the stone found"


class Keyword:
    """An encoder in which a text about the stone points one way and any other text another."""

    embedding_model_name = "fake"

    def encode(self, texts, query=False):
        return np.array([[1.0, 0.0] if "stone" in t else [0.0, 1.0] for t in texts])


def link(url, anchor, region="main"):
    return Link(url, anchor, "", region)


def page(url, links=(), title="", text="The text of the page."):
    return Page(url, url, title, text, "static", list(links), 0.0)


class Web:
    """The pages a run can fetch, by URL; any other URL fails. Records what was fetched."""

    def __init__(self, monkeypatch, *pages):
        self.pages = {p.url: p for p in pages}
        self.fetched = []
        self.texts = []
        monkeypatch.setattr(reading, "fetch_page", self.fetch)
        monkeypatch.setattr(reading, "intake", self.intake)

    def fetch(self, url):
        self.fetched.append(url)
        if url not in self.pages:
            raise FetchError(url)
        return self.pages[url]

    def intake(self, model, brief, question, hit, text, first_id, step, from_snippet=False):
        self.texts.append(text)
        found = claim(first_id, hit["url"])
        found.title, found.from_snippet = hit["title"], from_snippet
        return [found]


def follow(corpus, hits, claims=(), per_query=1, seen=None):
    """One search that finds `hits`, and the reading of what it picks."""
    seen = set() if seen is None else seen
    picked = search(
        Search(hits),
        ["q"],
        per_query,
        seen,
        Keyword(),
        QUESTION,
        corpus=corpus,
        claims=list(claims),
    )
    read(None, Keyword(), "brief", QUESTION, picked, 1, 1, "s", Stream(), corpus)
    return picked


def result(url):
    return {"url": url, "title": "start", "description": "", "snippets": []}


def test_a_run_follows_a_related_link_that_matches_a_question_and_skips_the_menu(monkeypatch):
    start = page(
        "https://a.org/start",
        [
            link("https://b.org/the-stone-found", "The stone found", "related"),
            link("https://c.org/stone-menu", "Stone", "nav"),
            link("https://d.org/taxes", "Tax rules"),
        ],
    )
    web = Web(monkeypatch, start, page("https://b.org/the-stone-found"))
    corpus, seen = Corpus(), set()

    follow(corpus, [result(start.url)], seen=seen)
    picked = follow(corpus, [result(start.url)], seen=seen)

    assert [hit["url"] for hit in picked] == ["https://b.org/the-stone-found"]
    assert picked[0]["depth"] == 1
    assert web.fetched == [start.url, "https://b.org/the-stone-found"]
    assert set(corpus.leads) == {"https://b.org/the-stone-found", "https://d.org/taxes"}


def test_a_run_reads_no_more_than_the_pages_per_site(monkeypatch):
    start = page("https://a.org/start", [link("https://b.org/stone", "The stone found")])
    web = Web(monkeypatch, start, page("https://b.org/stone"))
    corpus = Corpus()
    follow(corpus, [result(start.url)])
    corpus.pages["b.org"] = PAGES_PER_SITE

    picked = follow(corpus, [result("https://x.org/other")])

    assert [hit["url"] for hit in picked] == ["https://x.org/other"]
    assert "https://b.org/stone" not in web.fetched
    # A search result of that site is skipped as well.
    assert follow(corpus, [result("https://b.org/more")]) == []


def test_a_lead_below_the_minimum_is_not_read_however_thin_the_search(monkeypatch):
    start = page("https://a.org/start", [link("https://d.org/taxes", "Tax rules")])
    web = Web(monkeypatch, start)
    corpus = Corpus()
    follow(corpus, [result(start.url)])

    assert follow(corpus, []) == []
    assert web.fetched == [start.url]


def test_a_lead_whose_page_cannot_be_read_gives_no_claim_but_a_result_falls_back(monkeypatch):
    web = Web(monkeypatch)
    lead = {"url": "https://b.org/gone", "title": "anchor", "description": "what a page says"}
    snippet = {"url": "https://x.org/gone", "title": "t", "description": "the snippet"}

    claims = read(
        None, Keyword(), "brief", QUESTION, [{**lead, "depth": 1}], 1, 1, "s", Stream(), Corpus()
    )
    assert claims == [] and web.texts == []

    claims = read(None, Keyword(), "brief", QUESTION, [snippet], 1, 1, "s", Stream(), Corpus())
    assert web.texts == ["the snippet"]
    assert claims[0].from_snippet


def test_a_lead_takes_the_title_of_the_page_it_led_to(monkeypatch):
    Web(monkeypatch, page("https://b.org/stone", title="Stone find, 1904"))
    lead = {"url": "https://b.org/stone", "title": "anchor", "description": "", "depth": 1}

    claims = read(None, Keyword(), "brief", QUESTION, [lead], 1, 1, "s", Stream(), Corpus())

    assert claims[0].title == "Stone find, 1904"


def test_robots_txt_refusing_a_lead_keeps_it_unfetched_but_not_a_result_of_its_host(monkeypatch):
    start = page("https://a.org/start", [link("https://b.org/private/stone", "The stone found")])
    web = Web(monkeypatch, start, page("https://b.org/result"))
    monkeypatch.setattr(fetch, "allowed", lambda url: "/private/" not in url)
    corpus = Corpus()
    follow(corpus, [result(start.url)])

    assert follow(corpus, []) == []
    assert "https://b.org/private/stone" not in corpus.leads
    assert follow(corpus, [result("https://b.org/result")])
    assert web.fetched == [start.url, "https://b.org/result"]
    # Dropped for good: another page linking to it does not bring it back.
    corpus.add_leads(
        Keyword(), page("https://e.org/x", [link("https://b.org/private/stone", "stone")]), 1
    )
    assert corpus.leads == {}


def serve(monkeypatch, *answers):
    """Answer each request for a robots.txt with the next of `answers`, a response or an error."""
    asked = []

    def get(url, **kwargs):
        asked.append((url, kwargs["headers"]["User-Agent"]))
        answer = answers[len(asked) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(httpx, "get", get)
    resolves_to(monkeypatch, "93.184.216.34")
    return asked


def resolves_to(monkeypatch, *addresses):
    monkeypatch.setattr(
        fetch.socket,
        "getaddrinfo",
        lambda host, port: [(None, None, None, "", (address, 0)) for address in addresses],
    )


def test_a_robots_txt_redirect_is_followed_only_to_a_public_host(monkeypatch):
    moved = httpx.Response(301, headers={"location": "https://c.org/robots.txt"})
    asked = serve(monkeypatch, moved, httpx.Response(200, text="User-agent: *\nDisallow: /no/\n"))

    assert not allowed("https://b.org/no/page")
    assert [url for url, _ in asked] == ["https://b.org/robots.txt", "https://c.org/robots.txt"]

    inward = httpx.Response(302, headers={"location": "http://internal.example/robots.txt"})
    asked = serve(monkeypatch, inward)
    monkeypatch.setattr(fetch, "public", lambda host: host != "internal.example")

    assert not allowed("https://d.org/page")
    assert [url for url, _ in asked] == ["https://d.org/robots.txt"]


def test_robots_txt_rules_apply_to_this_crawler(monkeypatch):
    asked = serve(monkeypatch, httpx.Response(200, text="User-agent: *\nDisallow: /private/\n"))

    assert not allowed("https://b.org/private/page")
    assert allowed("https://b.org/open")
    # Asked once for the host, with the crawler's user agent.
    assert asked == [("https://b.org/robots.txt", fetch.user_agent())]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (httpx.Response(404), True),
        (httpx.Response(403), True),
        (httpx.Response(500), False),
        (httpx.Response(503), False),
        (httpx.ConnectError("down"), False),
    ],
)
def test_a_missing_robots_txt_allows_and_an_unavailable_one_refuses(monkeypatch, answer, expected):
    serve(monkeypatch, answer)

    assert allowed("https://b.org/page") is expected


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.0.0.5",),
        ("169.254.169.254",),
        ("::1",),
        ("fe80::1%eth0",),
        ("93.184.216.34", "192.168.1.1"),
    ],
)
def test_a_lead_on_a_host_that_is_not_public_is_refused_with_no_request(monkeypatch, addresses):
    asked = serve(monkeypatch)
    resolves_to(monkeypatch, *addresses)

    assert not allowed("https://intranet.example/admin")
    assert asked == []


def test_a_host_that_does_not_resolve_is_refused(monkeypatch):
    def fail(host, port):
        raise OSError("no such host")

    monkeypatch.setattr(fetch.socket, "getaddrinfo", fail)

    assert not allowed("https://nowhere.example/page")


def test_robots_txt_is_read_again_after_a_day(monkeypatch):
    open_rules = httpx.Response(200, text="User-agent: *\nAllow: /\n")
    closed_rules = httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    asked = serve(monkeypatch, open_rules, closed_rules)
    now = 1000.0
    monkeypatch.setattr(fetch.time, "time", lambda: now)

    assert allowed("https://b.org/page")
    now += fetch.ROBOTS_MAX_AGE_SECONDS - 1
    assert allowed("https://b.org/page")
    assert len(asked) == 1
    now += 1
    assert not allowed("https://b.org/page")
    assert len(asked) == 2


def test_a_lead_is_read_at_the_url_its_page_wrote_and_two_spellings_are_one_lead():
    def linking(url, target):
        return Page(url, url, "", "text", "static", [Link(target, "stone", "c", "related")], 0.0)

    encoder = Vectors({}, default=[1.0, 0.0])
    corpus = Corpus()
    corpus.add_leads(encoder, linking("https://a.org/x", "http://www.b.org/article/"), 1)
    corpus.add_leads(encoder, linking("https://a.org/y", "https://b.org/article"), 1)

    assert [lead.url for lead in corpus.leads.values()] == ["http://www.b.org/article/"]
    corpus.refuse("https://www.b.org/article")
    assert corpus.leads == {}


def test_a_lead_of_a_page_at_the_depth_limit_is_not_kept():
    links = [link("https://b.org/stone", "stone")]
    corpus = Corpus()

    corpus.add_leads(Keyword(), page("https://a.org/x", links), LEAD_DEPTH + 1)
    assert corpus.leads == {}

    corpus.add_leads(Keyword(), page("https://a.org/x", links), LEAD_DEPTH)
    assert [lead.depth for lead in corpus.leads.values()] == [LEAD_DEPTH]


def test_a_page_read_as_a_lead_at_depth_two_leaves_no_lead(monkeypatch):
    Web(monkeypatch, page("https://b.org/stone", [link("https://c.org/stone", "stone")]))
    lead = {"url": "https://b.org/stone", "title": "anchor", "description": "", "depth": LEAD_DEPTH}
    corpus = Corpus()

    read(None, Keyword(), "brief", QUESTION, [lead], 1, 1, "s", Stream(), corpus)

    assert corpus.leads == {}


def test_a_page_keeps_related_links_first_none_of_the_menu_and_a_url_once():
    links = [
        link("https://b.org/1", "one"),
        link("https://b.org/nav", "n", "nav"),
        link("https://b.org/foot", "f", "footer"),
        link("https://b.org/2", "two", "related"),
        link("https://a.org/x", "self"),
    ]
    corpus = Corpus()
    corpus.add_leads(Keyword(), page("https://a.org/x", links), 1)
    first = corpus.leads["https://b.org/1"]

    corpus.add_leads(Keyword(), page("https://z.org/x", [link("https://b.org/1", "again")]), 2)

    assert list(corpus.leads) == ["https://b.org/2", "https://b.org/1"]
    assert corpus.leads["https://b.org/1"] is first


def test_a_related_link_outranks_an_otherwise_equal_link_in_the_text():
    corpus = Corpus()
    corpus.add_leads(Keyword(), page("https://a.org/x", [link("https://b.org/1", "stone")]), 1)
    corpus.add_leads(
        Keyword(), page("https://c.org/x", [link("https://d.org/2", "stone", "related")]), 1
    )

    found = corpus.leads_for(Keyword(), QUESTION, None, [], set(), 2)

    assert [hit["url"] for hit in found] == ["https://d.org/2", "https://b.org/1"]


def test_a_site_whose_claims_are_confirmed_outranks_one_that_is_not():
    corpus = Corpus()
    corpus.add_leads(Keyword(), page("https://a.org/x", [link("https://b.org/1", "stone")]), 1)
    corpus.add_leads(Keyword(), page("https://c.org/x", [link("https://d.org/2", "stone")]), 1)
    corpus.pages["d.org"] = 1
    confirmed = claim(1, "https://d.org/old")
    confirmed.corroborated_by = [2]

    found = corpus.leads_for(Keyword(), QUESTION, None, [confirmed], set(), 2)

    assert [hit["url"] for hit in found] == ["https://d.org/2", "https://b.org/1"]


def test_a_lead_a_claim_held_already_covers_ranks_below_a_new_one():
    corpus = Corpus()
    corpus.add_leads(Keyword(), page("https://a.org/x", [link("https://b.org/1", "stone")]), 1)
    corpus.leads["https://b.org/1"].vector = np.array([1.0, 1.0])
    corpus.add_leads(Keyword(), page("https://c.org/x", [link("https://d.org/2", "stone")]), 1)
    corpus.leads["https://d.org/2"].vector = np.array([1.0, -1.0])

    held = np.array([[1.0, 1.0]])
    found = corpus.leads_for(Keyword(), QUESTION, held, [], set(), 2)

    assert [hit["url"] for hit in found] == ["https://d.org/2", "https://b.org/1"]


def confirmed(site, count):
    claims: list[Claim] = []
    for i in range(count):
        found = claim(i, f"https://{site}/{i}")
        found.corroborated_by = [99]
        claims.append(found)
    return claims


def test_a_site_search_fires_once_per_question_and_site_at_or_above_the_affinity():
    corpus = Corpus()
    corpus.pages["a.org"] = 2
    claims = confirmed("a.org", 2)
    assert SITE_AFFINITY <= 2 / 2

    assert corpus.site_query(1, "when", claims) == "site:a.org when"
    assert corpus.site_query(1, "when", claims) is None
    assert corpus.site_query(2, "where", claims) == "site:a.org where"


def test_a_site_search_needs_the_affinity_and_a_site_with_pages_left():
    corpus = Corpus()
    corpus.pages["a.org"] = 4
    assert corpus.site_query(1, "when", confirmed("a.org", 3)) is None

    corpus.pages["b.org"] = PAGES_PER_SITE
    assert corpus.site_query(1, "when", confirmed("b.org", PAGES_PER_SITE)) is None


def test_a_run_makes_no_more_than_the_site_searches():
    corpus = Corpus()
    claims = []
    for site in ("a.org", "b.org", "c.org"):
        corpus.pages[site] = 2
        claims += confirmed(site, 2)

    found = [corpus.site_query(i, "when", claims) for i in range(SITE_SEARCHES + 1)]

    assert all(found[:SITE_SEARCHES]) and found[SITE_SEARCHES] is None
