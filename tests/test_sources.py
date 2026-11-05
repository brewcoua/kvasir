"""Source trust: tiers, the order a host resolves in, and what is asked of Wikidata and kept."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time

import httpx
import pytest

from kvasir.config import SourceSettings
from kvasir.research import fetch, sources
from kvasir.research.sources import Source, Sources, SourceStore, parse_list, parse_rsp, tier

DAY = 86400
NS = "http://www.wikidata.org/entity/"
NEWSPAPER, ORG, STANDARDS = "Q11032", "Q43229", "Q1328899"
INTERNATIONAL, NGO, WIKI, AI = "Q484652", "Q79913", "Q171", "Q125361038"
UNIVERSITY, ENCYCLOPEDIA, GOVERNMENT = "Q3918", "Q5292", "Q327333"


class Wdqs:
    """A fake query service: answers `{host: [(item, label, roots)]}` as WDQS JSON, and keeps the
    queries and headers it was sent. `gate`, when set, holds every answer until it is set."""

    def __init__(self, rows=None, status=200, headers=None, names=None):
        self.rows = rows or {}
        # The name a host is listed under on Wikidata, when not its own.
        self.names = names or {}
        self.status = status
        self.headers = headers or {}
        self.queries = []
        self.sent = []
        self.gate = None

    def post(self, url, *, data, headers, timeout):
        self.queries.append(data["query"])
        self.sent.append(headers)
        if self.gate:
            self.gate.wait(5)
        if self.status != 200:
            return httpx.Response(self.status, headers=self.headers)
        bindings = []
        for host, items in self.rows.items():
            if f'("{host}" "{self.names.get(host, host)}")' not in data["query"]:
                continue
            for item, label, roots in items:
                for root in roots:
                    row = {
                        "h": {"type": "literal", "value": host},
                        "i": {"type": "uri", "value": NS + item},
                        "root": {"type": "uri", "value": NS + root},
                    }
                    if label:
                        row["l"] = {"type": "literal", "value": label, "xml:lang": "en"}
                    bindings.append(row)
        return httpx.Response(200, json={"head": {}, "results": {"bindings": bindings}})


@pytest.fixture
def wdqs(monkeypatch):
    def serve(**kwargs):
        service = Wdqs(**kwargs)
        monkeypatch.setattr(sources.httpx, "post", service.post)
        return service

    return serve


@pytest.fixture
def store(tmp_path):
    return SourceStore(tmp_path / "sources.db")


def service(store, tmp_path, **settings):
    settings.setdefault("lists", ())
    return Sources(SourceSettings(enabled=True, **settings), store, tmp_path)


def one(svc, host, deadline=5.0):
    return svc.classify([host], deadline)[host]


@pytest.mark.parametrize(
    ("kind", "tags", "expected"),
    [
        ("organization", {"ai_generated"}, "avoid"),
        ("news", {"content_farm"}, "avoid"),
        ("government", {"predatory"}, "avoid"),
        ("reference", {"rsp_deprecated"}, "avoid"),
        ("unknown", {"deny"}, "avoid"),
        ("news", {"rsp_unreliable"}, "low"),
        ("government", {"rsp_unreliable"}, "low"),
        ("government", set(), "high"),
        ("intergovernmental", set(), "high"),
        ("academic", {"official"}, "high"),
        ("standards", set(), "high"),
        ("news", {"rsp_reliable"}, "high"),
        ("unknown", {"allow"}, "high"),
        ("news", set(), "normal"),
        ("reference", {"community", "official"}, "normal"),
        ("organization", {"official"}, "normal"),
        ("unknown", set(), "unknown"),
    ],
)
def test_tier_follows_the_plans_table(kind, tags, expected):
    assert tier(kind, tags) == expected
    assert Source(host="x.example", kind=kind, tags=frozenset(tags)).tier == expected


@pytest.mark.parametrize(
    ("host", "kind"),
    [
        ("who.int", "intergovernmental"),
        ("commission.europa.eu", "intergovernmental"),
        ("cdc.gov", "government"),
        ("army.mil", "government"),
        ("education.gouv.fr", "government"),
        ("service.gov.uk", "government"),
        ("canada.gc.ca", "government"),
        ("mit.edu", "academic"),
        ("ox.ac.uk", "academic"),
        ("unsw.edu.au", "academic"),
    ],
)
def test_suffix_rules_set_the_kind_without_asking_wikidata(store, tmp_path, wdqs, host, kind):
    wdqs()
    found = one(service(store, tmp_path), host)

    assert (found.kind, found.origin) == (kind, "rule")
    assert found.tier == "high"


def test_a_rule_host_does_not_wait_for_wikidata(store, tmp_path, wdqs):
    asked = wdqs()
    asked.gate = threading.Event()
    try:
        start = time.monotonic()
        assert one(service(store, tmp_path), "cdc.gov", deadline=5).kind == "government"
        assert time.monotonic() - start < 1
    finally:
        asked.gate.set()


def test_wikidata_cannot_change_the_kind_a_rule_set(store, tmp_path, wdqs):
    # europa.eu is the Health-EU portal on Wikidata, an organization; the suffix rule wins.
    wdqs(rows={"europa.eu": [("Q684245", "Health-EU portal", [ORG])]})
    svc = service(store, tmp_path)
    one(svc, "europa.eu")
    time.sleep(0.2)

    found = one(svc, "europa.eu")

    assert (found.kind, found.origin) == ("intergovernmental", "rule")
    assert (found.label, found.qid) == ("Health-EU portal", "Q684245")


def test_iso_is_standards_though_it_reaches_international_organization(store, tmp_path, wdqs):
    wdqs(rows={"iso.org": [("Q15028", "ISO", [STANDARDS, INTERNATIONAL, NGO, ORG])]})

    found = one(service(store, tmp_path), "iso.org")

    assert (found.kind, found.qid, found.label) == ("standards", "Q15028", "ISO")
    assert "official" in found.tags


def test_who_is_intergovernmental(store, tmp_path, wdqs):
    wdqs(rows={"example-who.org": [("Q7817", "WHO", [INTERNATIONAL, ORG])]})

    assert one(service(store, tmp_path), "example-who.org").kind == "intergovernmental"


def test_a_host_matching_several_items_takes_the_best_kind_and_all_tags(store, tmp_path, wdqs):
    wdqs(
        rows={
            "nytimes.com": [
                ("Q9684", "The New York Times", [NEWSPAPER, ORG]),
                ("Q999", "nytimes.com", [ORG, WIKI]),
            ]
        }
    )

    found = one(service(store, tmp_path), "nytimes.com")

    assert (found.kind, found.qid, found.label) == ("news", "Q9684", "The New York Times")
    assert {"official", "community"} <= found.tags


def test_a_subdomain_is_asked_with_its_registrable_domain(store, tmp_path, wdqs):
    asked = wdqs(
        rows={"en.wikipedia.org": [("Q328", "Wikipedia", [ENCYCLOPEDIA, WIKI])]},
        names={"en.wikipedia.org": "wikipedia.org"},
    )

    found = one(service(store, tmp_path), "en.wikipedia.org")

    assert '("en.wikipedia.org" "en.wikipedia.org")' in asked.queries[0]
    assert '("en.wikipedia.org" "wikipedia.org")' not in asked.queries[0]
    assert '("en.wikipedia.org" "wikipedia.org")' in asked.queries[1]
    assert '("en.wikipedia.org" <https://www.wikipedia.org/>)' in asked.queries[1]
    assert (found.kind, found.tier) == ("reference", "normal")
    assert "community" in found.tags


def test_a_subdomain_with_its_own_item_does_not_take_its_parents(store, tmp_path, wdqs):
    asked = wdqs(
        rows={
            "docs.example.org": [("Q2", "Docs", [ENCYCLOPEDIA])],
            "example.org": [("Q1", "Example", [ORG])],
        }
    )

    found = one(service(store, tmp_path), "docs.example.org")

    assert (found.kind, found.qid) == ("reference", "Q2")
    assert len(asked.queries) == 1


def test_international_organization_is_decided_per_item(store, tmp_path, wdqs):
    wdqs(
        rows={
            "mixed.example": [
                ("Q1", "A NGO", [INTERNATIONAL, NGO]),
                ("Q2", "An IGO", [INTERNATIONAL]),
            ]
        }
    )

    found = one(service(store, tmp_path), "mixed.example")

    assert (found.kind, found.qid) == ("intergovernmental", "Q2")


def test_each_name_is_asked_as_eight_urls(store, tmp_path, wdqs):
    asked = wdqs()

    one(service(store, tmp_path), "reintech.io")

    assert asked.queries[0].count('("reintech.io" <') == 8


def test_a_wikidata_ai_site_is_avoid(store, tmp_path, wdqs):
    wdqs(rows={"grokipedia.example": [("Q136410803", "Grokipedia", [ENCYCLOPEDIA, AI])]})

    assert one(service(store, tmp_path), "grokipedia.example").tier == "avoid"


def test_the_request_names_this_service(store, tmp_path, wdqs):
    asked = wdqs()

    one(service(store, tmp_path), "reintech.io")

    assert asked.sent[0]["User-Agent"] == fetch.user_agent()


def test_an_unknown_host_is_unknown_and_remembered_as_a_miss(store, tmp_path, wdqs):
    asked = wdqs()
    svc = service(store, tmp_path)

    assert one(svc, "reintech.io") == Source(host="reintech.io", kind="unknown")
    time.sleep(0.2)
    one(svc, "reintech.io")

    assert len(asked.queries) == 1
    assert store.get("reintech.io", 30, 7).match is None


def test_hosts_are_normalized_and_an_ip_is_unknown(store, tmp_path, wdqs):
    asked = wdqs()

    found = service(store, tmp_path).classify(["WWW.Reintech.io:443", "10.0.0.1"], 5)

    assert found["WWW.Reintech.io:443"].host == "reintech.io"
    assert found["10.0.0.1"].kind == "unknown"
    assert "10.0.0.1" not in asked.queries[0]


def test_hosts_are_asked_in_batches_of_fifty(store, tmp_path, wdqs):
    asked = wdqs()
    hosts = [f"site{n}.example" for n in range(120)]

    service(store, tmp_path).classify(hosts, 5)

    assert len(asked.queries) == 3


def test_two_asks_for_a_host_send_one_query(store, tmp_path, wdqs):
    asked = wdqs(rows={"nytimes.com": [("Q9684", "NYT", [NEWSPAPER])]})
    asked.gate = threading.Event()
    svc = service(store, tmp_path)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(one(svc, "nytimes.com"))) for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    asked.gate.set()
    for thread in threads:
        thread.join()

    assert len(asked.queries) == 1
    assert [r.kind for r in results] == ["news", "news"]


def test_past_the_deadline_a_host_is_unknown_and_known_by_the_next_ask(store, tmp_path, wdqs):
    asked = wdqs(rows={"nytimes.com": [("Q9684", "NYT", [NEWSPAPER])]})
    asked.gate = threading.Event()
    svc = service(store, tmp_path)

    start = time.monotonic()
    assert one(svc, "nytimes.com", deadline=0.1).kind == "unknown"
    assert time.monotonic() - start < 2
    asked.gate.set()
    time.sleep(0.3)

    assert one(svc, "nytimes.com", deadline=0.1).kind == "news"


def test_a_rate_limit_stores_no_miss_and_backs_off_by_retry_after(store, tmp_path, wdqs):
    asked = wdqs(status=429, headers={"Retry-After": "120"})
    svc = service(store, tmp_path)

    assert one(svc, "nytimes.com").kind == "unknown"
    assert store.get("nytimes.com", 30, 7) is None
    one(svc, "reintech.io")

    assert len(asked.queries) == 1
    assert 100 < svc._backoff_until - time.monotonic() <= 120


def test_a_failed_query_stores_no_miss(store, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sources.httpx, "post", fail)

    assert one(service(store, tmp_path), "nytimes.com").kind == "unknown"
    assert store.get("nytimes.com", 30, 7) is None


def _age(store, days):
    with store._connect() as db:
        db.execute("UPDATE sources SET fetched_at = fetched_at - ?", (days * DAY,))


def test_a_match_expires_after_ttl_days_and_a_miss_after_miss_ttl_days(store, tmp_path, wdqs):
    asked = wdqs(rows={"nytimes.com": [("Q9684", "NYT", [NEWSPAPER])]})
    svc = service(store, tmp_path)
    for host in ("nytimes.com", "reintech.io"):
        one(svc, host)
    time.sleep(0.2)

    _age(store, 8)
    assert store.get("nytimes.com", 30, 7) is not None
    assert store.get("reintech.io", 30, 7) is None
    _age(store, 23)
    assert store.get("nytimes.com", 30, 7) is None

    one(svc, "nytimes.com")
    assert len(asked.queries) == 3


def test_with_wikidata_off_nothing_is_asked_or_read_from_the_cache(store, tmp_path, wdqs):
    asked = wdqs(rows={"nytimes.com": [("Q9684", "NYT", [NEWSPAPER])]})
    store.put("nytimes.com", sources._Match("news", frozenset(), "", ""))
    svc = service(store, tmp_path, wikidata=False)

    assert one(svc, "nytimes.com").kind == "unknown"
    assert one(svc, "cdc.gov").kind == "government"
    assert asked.queries == []


def test_a_mark_sets_the_kind_or_the_tier(store, tmp_path, wdqs):
    wdqs()
    svc = service(store, tmp_path)
    svc.mark("www.Reintech.io", "deny")
    svc.mark("blog.example", "news")

    assert one(svc, "reintech.io").tier == "avoid"
    assert one(svc, "docs.reintech.io").origin == "mark"
    assert (one(svc, "blog.example").kind, one(svc, "blog.example").origin) == ("news", "mark")
    svc.unmark("reintech.io")
    assert one(svc, "reintech.io").origin == ""


def test_a_mark_must_be_allow_deny_or_a_kind(store, tmp_path):
    svc = service(store, tmp_path)

    for host, value in [
        ("reintech.io", "trusted"),
        ("reintech.io", "unknown"),
        ("10.0.0.1", "allow"),
    ]:
        with pytest.raises(ValueError):
            svc.mark(host, value)


def test_a_mark_beats_the_rules_and_the_lists(store, tmp_path, wdqs):
    wdqs()
    svc = service(store, tmp_path)
    svc.mark("cdc.gov", "deny")
    svc.mark("deepwiki.com", "allow")

    assert one(svc, "cdc.gov").tier == "avoid"
    assert one(svc, "deepwiki.com").tier == "high"


def test_config_deny_beats_a_mark_and_allow_beats_a_list(store, tmp_path, wdqs):
    wdqs()
    svc = service(store, tmp_path, deny=("Reintech.io",), allow=("grokipedia.com",))
    svc.mark("reintech.io", "allow")

    found = one(svc, "docs.reintech.io")
    assert (found.tier, found.origin) == ("avoid", "config")
    assert one(svc, "grokipedia.com").tier == "high"


def test_config_deny_beats_config_allow(store, tmp_path, wdqs):
    wdqs()

    assert (
        one(service(store, tmp_path, allow=("a.example",), deny=("a.example",)), "a.example").tier
        == "avoid"
    )


def test_a_mark_survives_cache_expiry(store, tmp_path, wdqs):
    wdqs(rows={"nytimes.com": [("Q9684", "NYT", [NEWSPAPER])]})
    first = service(store, tmp_path)
    first.mark("nytimes.com", "deny")
    first.mark("reintech.io", "academic")
    one(first, "other.example")
    time.sleep(0.2)
    _age(store, 365)
    with store._connect() as db:
        db.execute("UPDATE marks SET marked_at = marked_at - ?", (365 * DAY,))

    later = service(SourceStore(tmp_path / "sources.db"), tmp_path)

    assert one(later, "nytimes.com").tier == "avoid"
    assert one(later, "reintech.io").kind == "academic"
    assert store.get("other.example", 30, 7) is None


def test_a_fully_qualified_name_with_a_trailing_dot_is_the_same_host(store, tmp_path, wdqs):
    wdqs()
    svc = service(store, tmp_path, deny=("farm.example.",))

    assert one(svc, "deepwiki.com.").tier == "avoid"
    assert one(svc, "farm.example.").origin == "config"
    svc.mark("blog.example.", "news")
    assert one(svc, "blog.example").kind == "news"


def test_the_bundled_list_marks_ai_generated_sites_and_their_subdomains(store, tmp_path, wdqs):
    asked = wdqs()
    svc = service(store, tmp_path)

    assert one(svc, "deepwiki.com").tier == "avoid"
    assert one(svc, "foo.grokipedia.com").tier == "avoid"
    assert one(svc, "deepwiki.com").origin == "list"
    assert asked.queries == []


HOSTS_FILE = (
    "# a comment\n\n0.0.0.0 forkful.ai\r\n127.0.0.1\tcgdream.ai\r\nlocalhost\n"
    "0.0.0.0 tryleap.ai # ads\n"
)
UBLACKLIST = (
    "### a comment\n*://*.otter.ai/*\n*://krisp.ai/*\n/pinterest.+\\/racingtff/\n"
    "*://example.org/some/path/*\n"
)
CISA = (
    "Domain name,Domain type,Organization name\n"
    "29palms.gov,City,City of Twentynine Palms\n"
    'Abbeville.GOV,City,"Abbeville, LA"\n'
)
ADBLOCK = '! Title: x\ndomain.example##a[href*="y"]\n'


@pytest.mark.parametrize(
    ("text", "hosts"),
    [
        (HOSTS_FILE, {"forkful.ai", "cgdream.ai", "tryleap.ai"}),
        (UBLACKLIST, {"otter.ai", "krisp.ai"}),
        (CISA, {"29palms.gov", "abbeville.gov"}),
        (ADBLOCK, set()),
    ],
)
def test_lists_are_read_as_hosts_ublacklist_or_csv(text, hosts):
    assert parse_list(text) == hosts


def test_lists_are_downloaded_into_the_data_dir_and_label_their_hosts(
    store, tmp_path, monkeypatch, wdqs
):
    wdqs()
    gets = []

    def get(method, url, **kwargs):
        gets.append(url)
        return contextlib.nullcontext(httpx.Response(200, text=HOSTS_FILE if "ai" in url else CISA))

    monkeypatch.setattr(sources.httpx, "stream", get)
    svc = service(
        store,
        tmp_path,
        lists=(
            "ai_generated=https://lists.example/ai.txt",
            "government=https://lists.example/gov.csv",
        ),
    )

    svc.refresh_lists()
    found = svc.classify(["forkful.ai", "29palms.gov", "city.29palms.gov"], 5)
    svc.refresh_lists()

    assert found["forkful.ai"].tier == "avoid"
    assert found["29palms.gov"].origin == "list"
    assert found["29palms.gov"].kind == "government"
    assert found["city.29palms.gov"].kind == "government"
    assert len(gets) == 2 and len(list(tmp_path.glob("sources-*.txt"))) == 2


def test_a_list_older_than_ttl_days_is_fetched_again_and_a_failure_keeps_the_copy(
    store, tmp_path, monkeypatch, wdqs
):
    wdqs()
    answers = [httpx.Response(200, text=HOSTS_FILE), httpx.Response(503)]
    monkeypatch.setattr(
        sources.httpx,
        "stream",
        lambda method, url, **kwargs: contextlib.nullcontext(answers.pop(0)),
    )
    svc = service(store, tmp_path, lists=("ai_generated=https://lists.example/ai.txt",))
    svc.refresh_lists()
    for path in tmp_path.glob("sources-*.txt"):
        old = time.time() - 31 * DAY
        os.utime(path, (old, old))

    svc.refresh_lists()

    assert answers == []
    assert one(svc, "forkful.ai").tier == "avoid"


def test_a_list_with_an_unknown_label_is_ignored(store, tmp_path, monkeypatch, wdqs):
    wdqs()
    monkeypatch.setattr(
        sources.httpx, "get", lambda url, **kwargs: httpx.Response(200, text=HOSTS_FILE)
    )
    svc = service(store, tmp_path, lists=("shiny=https://lists.example/ai.txt",))

    svc.refresh_lists()

    assert one(svc, "forkful.ai").kind == "unknown"


def test_a_list_over_the_size_cap_or_not_utf8_is_skipped_without_raising(
    store, tmp_path, monkeypatch, wdqs
):
    wdqs()
    monkeypatch.setattr(sources, "MAX_LIST_BYTES", 10)
    monkeypatch.setattr(
        sources.httpx,
        "stream",
        lambda method, url, **kwargs: contextlib.nullcontext(httpx.Response(200, text=HOSTS_FILE)),
    )
    svc = service(store, tmp_path, lists=("ai_generated=https://lists.example/ai.txt",))
    svc.refresh_lists()
    assert list(tmp_path.glob("sources-*")) == []

    path = svc._list_path("https://lists.example/ai.txt")
    path.write_bytes(b"\xff\xfe\x00bad")
    svc.refresh_lists()

    assert one(svc, "forkful.ai").kind == "unknown"


@pytest.mark.parametrize("retry_after", ["nan", "inf", "-5", "soon"])
def test_a_retry_after_that_is_not_a_sane_number_is_clamped(store, tmp_path, wdqs, retry_after):
    wdqs(status=429, headers={"Retry-After": retry_after})
    svc = service(store, tmp_path)

    one(svc, "nytimes.com")

    assert svc._backoff_until - time.monotonic() <= sources.MAX_BACKOFF_SECONDS


def test_a_server_error_backs_off_briefly(store, tmp_path, wdqs):
    asked = wdqs(status=503)
    svc = service(store, tmp_path)

    one(svc, "nytimes.com")
    one(svc, "reintech.io")

    assert len(asked.queries) == 1


def row(status, name, uses=None, extra=""):
    cell = f"| {{{{WP:RSPUSES|{uses}}}}}" if uses else "| "
    return (
        f'|- class="s-{status}" id="{name}"\n| [[{name}]]\n| {{{{WP:RSPSTATUS|{status}}}}}\n'
        f"| [[WP:Reliable sources/Noticeboard/Archive 1#{name}|1]]\n| {{{{WP:RSPLAST|2021}}}}\n"
        f"| Summary of {name}.{extra}\n{cell}\n"
    )


# Modelled on the API's answer for the page's transcluded subpages, with rows cut to the columns
# that matter.
RSP = json.dumps(
    {
        "query": {
            "pages": [
                {
                    "title": "Wikipedia:RSPUSES",
                    "revisions": [{"slots": {"main": {"content": "x"}}}],
                },
                {"title": "Gone"},
                {
                    "title": "Wikipedia:Reliable sources/Perennial sources/1",
                    "revisions": [
                        {
                            "slots": {
                                "main": {
                                    "content": '{{WP:RSPTableFull}}\n{| class="wikitable"\n'
                                    + row("gr", "Reuters", "reuters.com")
                                    + row("nc", "Wired", "wired.com", " ({{duses|x.example}})")
                                    + row("gu", "Quora", "quora.com|www.quora.example")
                                    + row(
                                        "d",
                                        "Daily Mail",
                                        "dailymail.co.uk|pressreader.com/uk/daily-mail",
                                    )
                                    + row("b", "Spam", "spam.example")
                                    + row("gr", "Guardian", "theguardian.com")
                                    + row("nc", "Guardian blogs", "theguardian.com")
                                    + row("gr", "Peerage")
                                    + row("m", "Polygon", "polygon.com")
                                    + "|}"
                                }
                            }
                        }
                    ],
                },
            ]
        }
    }
)


def rsp_service(store, tmp_path, monkeypatch, wdqs, answer=RSP, **settings):
    wdqs()
    gets = []

    def get(method, url, **kwargs):
        gets.append((url, kwargs["headers"]))
        return contextlib.nullcontext(httpx.Response(200, text=answer))

    monkeypatch.setattr(sources.httpx, "stream", get)
    return service(store, tmp_path, **settings), gets


def test_the_perennial_sources_rows_give_a_tag_by_status_and_skip_what_has_no_site():
    assert parse_rsp(RSP) == {
        "reuters.com": "rsp_reliable",
        "wired.com": "rsp_mixed",
        "quora.com": "rsp_unreliable",
        "quora.example": "rsp_unreliable",
        "dailymail.co.uk": "rsp_deprecated",
        "spam.example": "rsp_deprecated",
        # Two rows, a status each: the less reliable holds.
        "theguardian.com": "rsp_mixed",
    }


def test_rsp_tags_give_the_tier_and_leave_the_kind_to_the_rest(store, tmp_path, monkeypatch, wdqs):
    svc, gets = rsp_service(store, tmp_path, monkeypatch, wdqs, rsp=True)
    svc.refresh_lists()

    found = svc.classify(
        [
            "reuters.com",
            "www.dailymail.co.uk",
            "m.quora.com",
            "wired.com",
            "agency.gov",
            "a.example",
        ],
        0,
    )

    assert gets[0][0] == sources.RSP_URL
    assert gets[0][1]["User-Agent"] == fetch.user_agent()
    assert found["reuters.com"].tags == {"rsp_reliable"}
    assert found["reuters.com"].tier == "high"
    assert found["reuters.com"].kind == "unknown"
    assert found["www.dailymail.co.uk"].tier == "avoid"
    assert found["m.quora.com"].tier == "low"
    assert found["wired.com"].tier == "unknown"
    assert found["agency.gov"].tags == set()
    assert found["a.example"].tags == set()


def test_an_rsp_tag_joins_the_kind_a_rule_or_a_list_gives(store, tmp_path, monkeypatch, wdqs):
    answer = RSP.replace("reuters.com", "agency.gov")
    svc, _ = rsp_service(
        store,
        tmp_path,
        monkeypatch,
        wdqs,
        answer,
        rsp=True,
        lists=("ai_generated=https://l.example/a",),
    )
    svc.refresh_lists()

    found = one(svc, "agency.gov", 0)

    assert (found.kind, found.tags, found.origin) == ("government", {"rsp_reliable"}, "rule")


def test_a_mark_or_a_config_entry_is_not_changed_by_the_rsp_overlay(
    store, tmp_path, monkeypatch, wdqs
):
    svc, _ = rsp_service(store, tmp_path, monkeypatch, wdqs, rsp=True, allow=("dailymail.co.uk",))
    svc.refresh_lists()
    svc.mark("quora.com", "news")

    assert one(svc, "dailymail.co.uk", 0).tags == {"allow"}
    assert one(svc, "quora.com", 0).tags == set()


def test_rsp_off_or_sources_off_fetches_and_tags_nothing(store, tmp_path, monkeypatch, wdqs):
    svc, gets = rsp_service(store, tmp_path, monkeypatch, wdqs)
    svc.refresh_lists()
    assert gets == [] and one(svc, "dailymail.co.uk", 0).tags == set()

    wdqs()
    off = Sources(SourceSettings(enabled=False, rsp=True, lists=()), store, tmp_path)
    off.refresh_lists()
    assert gets == [] and one(off, "dailymail.co.uk", 0).tags == set()


def test_an_unreadable_rsp_download_keeps_the_page_untagged(store, tmp_path, monkeypatch, wdqs):
    svc, _ = rsp_service(store, tmp_path, monkeypatch, wdqs, "<html>", rsp=True)
    svc.refresh_lists()

    assert one(svc, "dailymail.co.uk", 0).tags == set()
