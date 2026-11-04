"""Site icons: where they come from, what is refused, and what is remembered."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading

import httpx
import pytest

from kvasir.config import FaviconSettings
from kvasir.research import favicons, fetch
from kvasir.research.favicons import Favicons, FaviconStore, Icon, normalize_host

PNG = b"\x89PNG\r\n\x1a\n" + b"png" * 10
ICO = b"\x00\x00\x01\x00\x01\x00" + b"ico" * 10
GIF = b"GIF89a" + b"gif" * 10
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg" * 10
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"webp" * 10
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
DAY = 86400

PRIVATE = {"intranet.example", "internal.example"}


def ok(body=PNG, content_type="image/png"):
    return httpx.Response(200, headers={"content-type": content_type}, content=body)


def html(*links):
    return httpx.Response(
        200,
        headers={"content-type": "text/html"},
        content=f"<head>{''.join(links)}</head>".encode(),
    )


def redirect(to, status=302):
    return httpx.Response(status, headers={"location": to})


class Web:
    """The outbound requests: answers by URL, anything else a 404, and every URL asked is kept."""

    def __init__(self, routes):
        self.routes = routes
        self.asked = []
        self.headers = []

    def stream(self, method, url, **kwargs):
        self.asked.append(url)
        self.headers.append(kwargs["headers"])
        answer = self.routes.get(url, httpx.Response(404))
        if isinstance(answer, Exception):
            raise answer
        return contextlib.nullcontext(answer)


@pytest.fixture
def web(monkeypatch):
    def serve(routes):
        web = Web(routes)
        monkeypatch.setattr(favicons.httpx, "stream", web.stream)
        return web

    monkeypatch.setattr(fetch, "public", lambda host: host not in PRIVATE)
    monkeypatch.setattr(favicons, "_resolves", lambda host: True)
    return serve


@pytest.fixture
def store(tmp_path):
    return FaviconStore(tmp_path / "favicons.db")


def service(store, *providers, custom_url=""):
    return Favicons(FaviconSettings(providers=providers, custom_url=custom_url), store)


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("direct", "https://example.org/favicon.ico"),
        ("duckduckgo", "https://icons.duckduckgo.com/ip3/example.org.ico"),
        ("google", "https://www.google.com/s2/favicons?domain=example.org&sz=64"),
        ("custom", "https://icons.example/example.org.png"),
    ],
)
def test_each_provider_is_asked_for_the_host_and_its_icon_is_kept(web, store, provider, url):
    requests = web({url: ok()})
    icons = service(store, provider, custom_url="https://icons.example/{domain}.png")

    assert icons.find("www.Example.org:8443").icon == Icon(PNG, "image/png")
    assert requests.asked == [url]
    assert store.get("example.org", 30, 3).provider == provider


def test_the_request_names_this_service(web, store):
    requests = web({"https://example.org/favicon.ico": ok()})

    service(store, "direct").find("example.org")

    assert requests.headers[0]["User-Agent"] == fetch.user_agent()


def test_providers_are_tried_in_order_until_one_answers(web, store):
    requests = web(
        {
            "https://icons.duckduckgo.com/ip3/example.org.ico": httpx.Response(404, content=PNG),
            "https://www.google.com/s2/favicons?domain=example.org&sz=64": ok(ICO),
            "https://example.org/favicon.ico": ok(GIF),
        }
    )

    icon = service(store, "duckduckgo", "google", "direct").find("example.org").icon

    assert icon == Icon(ICO, "image/x-icon")
    assert requests.asked == [
        "https://icons.duckduckgo.com/ip3/example.org.ico",
        "https://www.google.com/s2/favicons?domain=example.org&sz=64",
    ]


@pytest.mark.parametrize(
    "answer",
    [
        # DuckDuckGo and Google send a placeholder image with the 404 (probed live).
        httpx.Response(404, headers={"content-type": "image/png"}, content=PNG),
        # DuckDuckGo sends an empty 200 for a site it does not know (probed live).
        httpx.Response(200, headers={"content-type": "image/x-icon"}, content=b""),
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>no</html>"),
        httpx.Response(200, headers={"content-type": "image/svg+xml"}, content=SVG),
        httpx.Response(200, headers={"content-type": "image/png"}, content=SVG),
        httpx.Response(200, content=b"\x00\x00\x02\x00\x01\x00cursor"),
    ],
)
def test_an_answer_that_is_not_an_icon_is_a_miss_and_is_remembered(web, store, answer):
    requests = web({"https://www.google.com/s2/favicons?domain=example.org&sz=64": answer})
    icons = service(store, "google")

    assert icons.find("example.org").icon is None
    assert icons.find("example.org").icon is None

    assert len(requests.asked) == 1
    assert store.get("example.org", 30, 3) is not None
    assert store.get("example.org", 30, 3).icon is None


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (PNG, "image/png"),
        (ICO, "image/x-icon"),
        (GIF, "image/gif"),
        (JPEG, "image/jpeg"),
        (WEBP, "image/webp"),
    ],
)
def test_the_content_type_comes_from_the_bytes_not_the_site(web, store, body, content_type):
    web({"https://example.org/favicon.ico": ok(body, "text/plain")})

    icon = service(store, "direct").find("example.org").icon

    assert icon == Icon(body, content_type)


def test_a_private_host_is_not_asked(web, store):
    requests = web({})

    assert service(store, "direct").find("intranet.example").icon is None
    assert requests.asked == []


def test_a_redirect_to_a_private_host_is_not_followed(web, store):
    requests = web({"https://example.org/favicon.ico": redirect("https://internal.example/i.png")})

    assert service(store, "direct").find("example.org").icon is None
    assert requests.asked[0] == "https://example.org/favicon.ico"
    assert "https://internal.example/i.png" not in requests.asked


def test_a_redirect_to_another_scheme_is_not_followed(web, store):
    requests = web({"https://example.org/favicon.ico": redirect("file:///etc/passwd")})

    assert service(store, "direct").find("example.org").icon is None
    assert requests.asked[0] == "https://example.org/favicon.ico"


def test_a_fourth_redirect_is_not_followed(web, store):
    url = "https://www.google.com/s2/favicons?domain=example.org&sz=64"
    hops = [url, "https://a.example/1", "https://a.example/2", "https://a.example/3"]
    routes = {
        hop: redirect(nxt)
        for hop, nxt in zip(hops, [*hops[1:], "https://a.example/4"], strict=True)
    }
    routes["https://a.example/4"] = ok()
    requests = web(routes)

    assert service(store, "google").find("example.org").icon is None
    assert requests.asked == hops


def test_an_icon_after_three_redirects_is_kept(web, store):
    url = "https://www.google.com/s2/favicons?domain=example.org&sz=64"
    web(
        {
            url: redirect("/a"),
            "https://www.google.com/a": redirect("https://b.example/b", 301),
            "https://b.example/b": redirect("https://c.example/c", 307),
            "https://c.example/c": ok(GIF),
        }
    )

    assert service(store, "google").find("example.org").icon == Icon(GIF, "image/gif")


def test_an_icon_over_the_size_limit_is_a_miss(web, store):
    web({"https://example.org/favicon.ico": ok(PNG + b"x" * favicons.MAX_BYTES)})

    assert service(store, "direct").find("example.org").icon is None


def test_the_body_is_read_no_further_than_the_limit(web, store):
    read = []

    class Endless(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(1000):
                read.append(1)
                yield PNG + b"x" * 65536

    web({"https://example.org/favicon.ico": httpx.Response(200, stream=Endless())})

    assert service(store, "direct").find("example.org").icon is None
    assert len(read) <= favicons.MAX_BYTES // 65536 + 1


def test_the_custom_provider_may_be_private_but_its_redirects_may_not(web, store):
    web({"http://intranet.example/example.org.png": ok()})
    template = "http://intranet.example/{domain}.png"

    assert service(store, "custom", custom_url=template).find("example.org").icon == Icon(
        PNG, "image/png"
    )

    requests = web(
        {"http://intranet.example/example.org.png": redirect("http://internal.example/x")}
    )
    assert service(store, "custom", custom_url=template).find("other.org").icon is None
    assert requests.asked == ["http://intranet.example/other.org.png"]

    requests = web({"http://intranet.example/third.org.png": redirect("https://a.example/i")})
    assert service(store, "custom", custom_url=template).find("third.org").icon is None
    assert requests.asked == [
        "http://intranet.example/third.org.png",
        "https://a.example/i",
    ]


def test_a_stored_icon_is_served_without_a_request(web, store):
    requests = web({"https://example.org/favicon.ico": ok()})
    icons = service(store, "direct")

    first = icons.find("example.org").icon
    second = icons.find("www.example.org").icon

    assert first == second == Icon(PNG, "image/png")
    assert len(requests.asked) == 1


def test_an_icon_and_a_miss_expire_after_their_own_ttl(store, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(favicons.time, "time", lambda: now)
    store.put("hit.org", Icon(PNG, "image/png"), "direct")
    store.put("miss.org", None, None)

    assert store.get("hit.org", 30, 3).icon == Icon(PNG, "image/png")
    assert store.get("miss.org", 30, 3).icon is None

    now += 3 * DAY + 1
    assert store.get("hit.org", 30, 3) is not None
    assert store.get("miss.org", 30, 3) is None

    now += 27 * DAY
    assert store.get("hit.org", 30, 3) is None


def test_an_expired_entry_is_fetched_again_and_overwritten(web, store, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(favicons.time, "time", lambda: now)
    routes = {"https://example.org/favicon.ico": ok(PNG)}
    requests = web(routes)
    icons = service(store, "direct")
    icons.find("example.org")

    now += 31 * DAY
    routes["https://example.org/favicon.ico"] = ok(GIF)

    assert icons.find("example.org").icon == Icon(GIF, "image/gif")
    assert len(requests.asked) == 2
    assert store.get("example.org", 30, 3).icon == Icon(GIF, "image/gif")


def test_flush_forgets_everything(store):
    store.put("a.org", Icon(PNG, "image/png"), "direct")
    store.put("b.org", None, None)

    store.flush()

    assert store.get("a.org", 30, 3) is None
    assert store.get("b.org", 30, 3) is None
    store.put("a.org", Icon(GIF, "image/gif"), "direct")
    assert store.get("a.org", 30, 3).icon == Icon(GIF, "image/gif")


def test_no_providers_means_no_request_and_no_write(web):
    requests = web({})
    # A store that fails on any use.
    icons = Favicons(FaviconSettings(providers=()), object())

    assert icons.find("example.org").icon is None
    assert requests.asked == []


def test_a_host_that_is_not_a_site_is_not_looked_up(web, store):
    requests = web({})

    assert service(store, "direct").find("192.168.0.1").icon is None
    assert requests.asked == []
    assert store.get("192.168.0.1", 30, 3) is None


def test_concurrent_lookups_of_one_host_make_one_request(web, store, monkeypatch):
    requests = web({"https://example.org/favicon.ico": ok()})
    asked, release = threading.Event(), threading.Event()
    stream = requests.stream

    def held(method, url, **kwargs):
        asked.set()
        release.wait(5)
        return stream(method, url, **kwargs)

    monkeypatch.setattr(favicons.httpx, "stream", held)
    icons = service(store, "direct")
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(icons.find("example.org").icon))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    assert asked.wait(5)
    # Long enough for the others to have joined the lookup in flight.
    threading.Event().wait(0.1)
    release.set()
    for thread in threads:
        thread.join(5)

    assert results == [Icon(PNG, "image/png")] * 4
    assert requests.asked == ["https://example.org/favicon.ico"]
    assert icons._flights == {}


def test_a_homepage_is_read_for_icon_links_when_favicon_ico_is_missing(web, store):
    requests = web(
        {
            "https://example.org/": html(
                '<link rel="stylesheet" href="/s.css">',
                '<link rel="ICON" href="/small.png" sizes="16x16">',
                '<link rel="shortcut icon" href="/big.png" sizes="16x16 64x64">',
                '<link rel="apple-touch-icon" href="https://cdn.example/t.png" sizes="180x180">',
                '<link rel="icon" href="/vector.svg">',
                '<link rel="icon" href="/typed" type="image/svg+xml">',
                '<link rel="icon" href="data:image/png;base64,AAAA">',
                '<link rel="icon" href="javascript:alert(1)">',
                '<link rel="icon" href="/last.png">',
            ),
            "https://cdn.example/t.png": httpx.Response(404),
            "https://example.org/big.png": ok(WEBP),
        }
    )

    assert service(store, "direct").find("example.org").icon == Icon(WEBP, "image/webp")
    assert requests.asked == [
        "https://example.org/favicon.ico",
        "https://example.org/",
        "https://cdn.example/t.png",
        "https://example.org/big.png",
    ]


def test_at_most_three_declared_icons_are_tried(web, store):
    links = [f'<link rel="icon" href="/{n}.png" sizes="{n}x{n}">' for n in (16, 32, 48, 64, 96)]
    requests = web({"https://example.org/": html(*links)})

    assert service(store, "direct").find("example.org").icon is None
    assert requests.asked[2:] == [f"https://example.org/{n}.png" for n in (96, 64, 48)]


def test_a_homepage_that_robots_txt_disallows_is_not_read(web, store, monkeypatch):
    requests = web({"https://example.org/": html('<link rel="icon" href="/i.png">')})
    monkeypatch.setattr(fetch, "allowed", lambda url: False)

    assert service(store, "direct").find("example.org").icon is None
    assert requests.asked == ["https://example.org/favicon.ico"]


def test_a_declared_icon_on_a_private_host_is_not_asked(web, store):
    requests = web(
        {"https://example.org/": html('<link rel="icon" href="https://internal.example/i.png">')}
    )

    assert service(store, "direct").find("example.org").icon is None
    assert "https://internal.example/i.png" not in requests.asked


def test_only_the_start_of_a_homepage_is_read(web, store):
    padding = b"<!-- " + b"x" * favicons.HOMEPAGE_BYTES + b" -->"
    late = b'<link rel="icon" href="/late.png">'
    requests = web(
        {
            "https://example.org/": httpx.Response(
                200,
                content=b'<link rel="icon" href="/early.png">' + padding + late,
            ),
            "https://example.org/early.png": ok(),
        }
    )

    assert service(store, "direct").find("example.org").icon == Icon(PNG, "image/png")
    assert "https://example.org/late.png" not in requests.asked


def test_a_transport_error_is_logged_and_nothing_is_remembered(web, store, caplog):
    requests = web(
        {
            "https://example.org/favicon.ico": httpx.ConnectError("down"),
            "https://example.org/": httpx.ConnectError("down"),
        }
    )
    icons = service(store, "direct")

    with caplog.at_level(logging.WARNING, logger=favicons.logger.name):
        assert icons.find("example.org") == favicons.Found(None, False)

    assert "example.org/favicon.ico" in caplog.text
    assert store.get("example.org", 30, 3) is None
    icons.find("example.org")
    assert len(requests.asked) == 4


def test_a_provider_that_answers_makes_a_miss_final_though_another_is_down(web, store):
    web(
        {
            "https://icons.duckduckgo.com/ip3/example.org.ico": httpx.ConnectError("down"),
            "https://www.google.com/s2/favicons?domain=example.org&sz=64": httpx.Response(404),
        }
    )

    assert service(store, "duckduckgo", "google").find("example.org") == favicons.Found(None, True)
    assert store.get("example.org", 30, 3).icon is None


def test_a_homepage_that_answers_makes_a_miss_final_though_favicon_ico_is_down(web, store):
    web(
        {
            "https://example.org/favicon.ico": httpx.ReadTimeout("slow"),
            "https://example.org/": html(),
        }
    )

    assert service(store, "direct").find("example.org") == favicons.Found(None, True)
    assert store.get("example.org", 30, 3) is not None


def test_a_host_the_public_check_refuses_is_a_final_miss(web, store):
    web({})

    assert service(store, "direct").find("intranet.example") == favicons.Found(None, True)
    assert store.get("intranet.example", 30, 3).icon is None


def test_a_store_that_fails_is_logged_and_does_not_raise(web, store, caplog):
    web({"https://example.org/favicon.ico": ok()})

    def fail(*args):
        raise sqlite3.OperationalError("disk full")

    store.get = fail
    store.put = fail
    with caplog.at_level(logging.WARNING, logger=favicons.logger.name):
        assert service(store, "direct").find("example.org").icon == Icon(PNG, "image/png")

    assert "disk full" in caplog.text


def test_a_lookup_past_its_deadline_stops_and_remembers_nothing(web, store, monkeypatch):
    now = 100.0
    monkeypatch.setattr(favicons.time, "monotonic", lambda: now)
    requests = web({})
    stream = requests.stream

    def slow(method, url, **kwargs):
        nonlocal now
        now += favicons.DEADLINE_SECONDS + 1
        return stream(method, url, **kwargs)

    monkeypatch.setattr(favicons.httpx, "stream", slow)

    assert service(store, "duckduckgo", "google").find("example.org").icon is None
    assert requests.asked == ["https://icons.duckduckgo.com/ip3/example.org.ico"]
    assert store.get("example.org", 30, 3) is None


class Chunks(httpx.SyncByteStream):
    """A body that arrives in pieces, calling `between` after each."""

    def __init__(self, *chunks, between=lambda: None):
        self.chunks = chunks
        self.between = between

    def __iter__(self):
        for chunk in self.chunks:
            yield chunk
            self.between()


def test_an_icon_still_arriving_at_its_attempts_end_is_not_kept(web, store, monkeypatch):
    now = 100.0
    monkeypatch.setattr(favicons.time, "monotonic", lambda: now)

    def later():
        nonlocal now
        now += favicons.ATTEMPT_SECONDS + 1

    web(
        {
            "https://www.google.com/s2/favicons?domain=example.org&sz=64": httpx.Response(
                200, stream=Chunks(PNG, PNG, between=later)
            )
        }
    )

    assert service(store, "google").find("example.org").icon is None


def test_an_icon_that_comes_compressed_anyway_is_not_read(web, store):
    requests = web(
        {
            "https://www.google.com/s2/favicons?domain=example.org&sz=64": httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=Chunks(PNG)
            )
        }
    )

    assert service(store, "google").find("example.org").icon is None
    assert requests.headers[0]["Accept-Encoding"] == "identity"


def test_a_name_that_does_not_resolve_is_not_remembered_as_a_miss(web, store, monkeypatch):
    web({})
    monkeypatch.setattr(fetch, "public", lambda host: False)
    monkeypatch.setattr(favicons, "_resolves", lambda host: False)

    found = service(store, "direct").find("example.org")

    assert found == favicons.Found(None, False)
    assert store.get("example.org", 30, 3) is None


def test_a_name_slow_to_resolve_does_not_hold_the_caller_past_the_deadline(web, store, monkeypatch):
    web({})
    release = threading.Event()
    monkeypatch.setattr(favicons, "DEADLINE_SECONDS", 0.05)

    def stuck(host):
        release.wait(5)
        return False

    monkeypatch.setattr(fetch, "public", stuck)
    icons = service(store, "direct")

    try:
        assert icons.find("example.org") == favicons.Found(None, False)
    finally:
        release.set()


def test_a_lookup_past_the_limit_in_progress_is_refused_without_a_request(web, store, monkeypatch):
    requests = web({})
    monkeypatch.setattr(favicons, "MAX_FLIGHTS", 0)

    assert service(store, "direct").find("example.org") == favicons.Found(None, False)
    assert requests.asked == []


def test_a_private_name_is_remembered_as_a_miss(web, store):
    web({})

    assert service(store, "direct").find("intranet.example") == favicons.Found(None, True)
    assert store.get("intranet.example", 30, 3) is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.org", "example.org"),
        ("EXAMPLE.org", "example.org"),
        ("www.example.org", "example.org"),
        ("WWW.Example.org:8443", "example.org"),
        ("sub.example.co.uk", "sub.example.co.uk"),
        ("a-b.example", "a-b.example"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("", None),
        ("localhost", None),
        ("www.localhost", None),
        ("127.0.0.1", None),
        ("127.1", None),
        ("[::1]", None),
        ("::1", None),
        ("[::1]:8080", None),
        ("2001:db8::1", None),
        ("example.org:abc", None),
        ("-a.example", None),
        ("a-.example", None),
        ("a..example", None),
        (".example.org", None),
        ("exa mple.org", None),
        ("exa_mple.org", None),
        ("a" * 64 + ".example", None),
        ("a" * 63 + ".example", "a" * 63 + ".example"),
        (".".join(["a" * 60] * 5) + ".org", None),
        ("‮.example", None),
    ],
)
def test_normalize_host(raw, expected):
    assert normalize_host(raw) == expected
