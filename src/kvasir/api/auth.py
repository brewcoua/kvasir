"""One shared token, `KVASIR_TOKEN`, guards every route but health, the login and the page.

A client sends `Authorization: Bearer <token>`. The page cannot on every request: `EventSource`
and `<img>` send no header. So `POST /v1/login` sets the token as a cookie, which is accepted too.
SameSite=Strict keeps another site's page from sending it.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from kvasir.config import Settings

COOKIE = "kvasir_token"
# A browser left alone for a month logs in again.
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600

router = APIRouter()


class Unauthorized(Exception):
    """No token, or the wrong one."""


class Login(BaseModel):
    token: str


def _valid(http: Request, given: str) -> bool:
    settings: Settings = http.app.state.settings
    return hmac.compare_digest(given.encode(), settings.token.encode())


def require_token(http: Request) -> None:
    """The dependency every guarded router is included with."""
    scheme, _, given = http.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        given = http.cookies.get(COOKIE, "")
    if not _valid(http, given):
        raise Unauthorized


@router.post("/v1/login", status_code=204)
async def login(body: Login, http: Request) -> Response:
    """Sets the cookie the page authenticates with. 401 for a wrong token."""
    if not _valid(http, body.token):
        raise Unauthorized
    response = Response(status_code=204)
    response.set_cookie(
        COOKIE,
        body.token,
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="strict",
        # Behind a proxy that ends TLS the scheme reads http, and the cookie is sent without it.
        secure=http.url.scheme == "https",
    )
    return response
