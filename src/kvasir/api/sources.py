"""What kind of site a host is and how far to trust it, and the marks a user sets on hosts. The
routes are sync, so a lookup, which blocks on the network and SQLite, runs on a thread."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from kvasir.research.sources import DEADLINE_SECONDS, Sources, info, normalize
from kvasir.schemas.results import SourceInfo

router = APIRouter()


class SourcesError(Exception):
    """A request the source routes cannot serve, with the status it answers."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


_LOCKED = "{} is set in the config, so a mark cannot change it"


class Mark(BaseModel):
    # `allow`, `deny` or a kind other than `unknown`.
    value: str


def _sources(http: Request) -> Sources:
    sources: Sources | None = http.app.state.sources
    if sources is None:
        raise SourcesError(404, "sources are off: set KVASIR_SOURCES_ENABLED=true")
    return sources


def _site(sources: Sources, host: str, deadline: float = DEADLINE_SECONDS) -> SourceInfo:
    return info(sources.classify([host], deadline)[host])


@router.get("/v1/sources/{host}")
def source(http: Request, host: str) -> SourceInfo:
    """The kind, tags and trust tier of a site, asking Wikidata when nothing else decides it."""
    if normalize(host) is None:
        raise SourcesError(422, f"{host!r} is not a host name")
    return _site(_sources(http), host)


@router.delete("/v1/sources", status_code=204)
def flush(http: Request) -> None:
    """Forgets the cached Wikidata answers. Marks and the config stay."""
    _sources(http).flush()


@router.put("/v1/sources/{host}/mark")
def mark(http: Request, host: str, body: Mark) -> SourceInfo:
    """Trusts, blocks or classifies a site, until cleared. A host the config sets answers 409."""
    sources = _sources(http)
    if sources.locked(host):
        raise SourcesError(409, _LOCKED.format(host))
    try:
        sources.mark(host, body.value)
    except ValueError as exc:
        raise SourcesError(422, str(exc)) from exc
    return _site(sources, host, deadline=0)


@router.delete("/v1/sources/{host}/mark", status_code=204)
def unmark(http: Request, host: str) -> None:
    sources = _sources(http)
    # A mark that outlived a later config entry can still be cleared.
    if sources.locked(host) and not sources.marked(host):
        raise SourcesError(409, _LOCKED.format(host))
    sources.unmark(host)
