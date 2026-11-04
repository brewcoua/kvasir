"""Site icons, served from the cache that fills itself on the first request for a site. The routes
are sync, so the lookup, which blocks on the network and SQLite, runs on a thread."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from kvasir.research.favicons import Favicons

router = APIRouter()

# An icon is final for a week in the browser. A missing one is asked again after a day, or after a
# minute when the lookup could not tell, so an outage does not hide icons for long.
ICON_AGE = 604800
MISS_AGE = 86400
RETRY_AGE = 60


def _miss(age: int) -> Response:
    # Returned, not raised: an error response would lose these headers.
    return Response(
        status_code=404,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": f"public, max-age={age}"},
    )


@router.get(
    "/v1/favicons/{host}",
    response_class=Response,
    responses={
        200: {"content": {"image/*": {}}, "description": "The site's icon."},
        404: {"description": "No icon for the host, or not now."},
    },
)
def favicon(http: Request, host: str) -> Response:
    """The icon of a site. The type is the one the image's own bytes show, and the response is
    locked down so a hostile image cannot run anything from this origin."""
    favicons: Favicons | None = http.app.state.favicons
    if favicons is None:
        return _miss(MISS_AGE)
    found = favicons.find(host)
    if found.icon is None:
        return _miss(MISS_AGE if found.final else RETRY_AGE)
    return Response(
        found.icon.body,
        media_type=found.icon.content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": f"public, max-age={ICON_AGE}",
        },
    )


@router.delete("/v1/favicons", status_code=204)
def flush(http: Request) -> None:
    """Forgets every icon, and frees its disk space."""
    favicons: Favicons | None = http.app.state.favicons
    if favicons is not None:
        favicons.flush()
