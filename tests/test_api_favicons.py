"""The favicon routes, with the providers faked and no request leaving the process."""

import contextlib

import httpx
import pytest
from fastapi.testclient import TestClient

from kvasir.api.app import app
from kvasir.research import favicons, fetch
from test_main import ENVIRONMENT

PNG = b"\x89PNG\r\n\x1a\n" + b"png" * 10
GIF = b"GIF89a" + b"gif" * 10
URL = "https://example.org/favicon.ico"


class Web:
    def __init__(self):
        self.routes = {}
        self.asked = []

    def stream(self, method, url, **kwargs):
        self.asked.append(url)
        answer = self.routes.get(url, httpx.Response(404))
        if isinstance(answer, Exception):
            raise answer
        return contextlib.nullcontext(answer)


@pytest.fixture
def web(monkeypatch):
    web = Web()
    monkeypatch.setattr(favicons.httpx, "stream", web.stream)
    monkeypatch.setattr(fetch, "public", lambda host: host != "intranet.example")
    monkeypatch.setattr(favicons, "_resolves", lambda host: True)
    return web


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def make_client(monkeypatch, data):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KVASIR_DATA_DIR", str(data))

    @contextlib.contextmanager
    def make():
        with TestClient(app) as client:
            yield client

    return make


@pytest.fixture
def client(make_client, web):
    with make_client() as client:
        yield client


def ok(body=PNG):
    return httpx.Response(200, content=body)


def test_an_icon_is_served_as_bytes_with_the_security_headers(client, web):
    web.routes[URL] = ok()

    response = client.get("/v1/favicons/example.org")

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["cache-control"] == "public, max-age=604800"


def test_the_content_type_comes_from_the_magic_bytes(client, web):
    web.routes[URL] = httpx.Response(200, headers={"content-type": "text/html"}, content=GIF)

    assert client.get("/v1/favicons/example.org").headers["content-type"] == "image/gif"


def test_a_site_without_an_icon_is_a_404_the_browser_keeps_for_a_day(client, web):
    response = client.get("/v1/favicons/example.org")

    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost", "[::1]", "a b.com", "%2e%2e", "intranet.example"]
)
def test_a_host_that_is_not_a_public_site_is_a_404_without_a_request(client, web, host):
    response = client.get(f"/v1/favicons/{host}")

    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert web.asked == []


def test_an_outage_is_a_404_asked_again_soon_and_not_remembered(client, web):
    web.routes[URL] = httpx.ConnectError("down")
    web.routes["https://example.org/"] = httpx.ConnectError("down")

    response = client.get("/v1/favicons/example.org")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "public, max-age=60"

    web.routes[URL] = ok()
    assert client.get("/v1/favicons/example.org").status_code == 200


def test_no_providers_is_a_404_with_no_request_and_no_database(make_client, web, monkeypatch, data):
    monkeypatch.setenv("KVASIR_FAVICONS_PROVIDERS", "")
    web.routes[URL] = ok()

    with make_client() as client:
        response = client.get("/v1/favicons/example.org")
        assert response.status_code == 404
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.delete("/v1/favicons").status_code == 204

    assert web.asked == []
    assert not (data / "favicons.db").exists()


def test_flush_forgets_icons_so_the_next_request_fetches_again(client, web):
    web.routes[URL] = ok()
    client.get("/v1/favicons/example.org")
    client.get("/v1/favicons/example.org")
    assert web.asked == [URL]

    assert client.delete("/v1/favicons").status_code == 204
    web.routes[URL] = ok(GIF)

    response = client.get("/v1/favicons/example.org")
    assert response.content == GIF
    assert web.asked == [URL, URL]
