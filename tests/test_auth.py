"""The token: every route but health, the login and the page needs it, as a header or a cookie."""

import re

import pytest
from fastapi.testclient import TestClient

from fakes import AUTH
from kvasir.api.app import app
from test_main import ENVIRONMENT


@pytest.fixture
def anonymous(monkeypatch, tmp_path):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "token"},
        {"Authorization": "Basic token"},
    ],
)
def test_a_guarded_route_refuses_without_the_token(anonymous, headers):
    response = anonymous.get("/v1/runs", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"message": "missing or wrong token"}


def test_the_bearer_token_is_accepted(anonymous):
    assert anonymous.get("/v1/runs", headers=AUTH).status_code == 200


def test_every_api_route_is_guarded(anonymous):
    """Asked of each route in the schema, with `x` for each path parameter. The token is checked
    before the body is, so no route needs a valid one to answer 401."""
    asked = 0
    for path, operations in anonymous.get("/openapi.json").json()["paths"].items():
        if path in ("/healthz", "/readyz", "/v1/login"):
            continue
        for method in operations:
            response = anonymous.request(method, re.sub(r"\{[^}]+\}", "x", path))
            assert response.status_code == 401, f"{method.upper()} {path}"
            asked += 1
    assert asked > 10


def test_health_and_the_page_need_no_token(anonymous):
    assert anonymous.get("/healthz").status_code == 200
    assert anonymous.get("/").status_code != 401


def test_a_wrong_login_sets_no_cookie(anonymous):
    response = anonymous.post("/v1/login", json={"token": "wrong"})

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert anonymous.get("/v1/runs").status_code == 401


def test_the_login_cookie_stands_in_for_the_header(anonymous):
    response = anonymous.post("/v1/login", json={"token": "token"})

    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("kvasir_token=token;")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    # The test client speaks http, and a Secure cookie would never come back over it.
    assert "Secure" not in cookie
    assert anonymous.get("/v1/runs").status_code == 200


def test_a_wrong_header_falls_back_to_no_cookie(anonymous):
    anonymous.post("/v1/login", json={"token": "token"})

    assert anonymous.get("/v1/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401
