"""Site icons, fetched on demand and kept in SQLite so a site is asked for its icon once a month.

Public icons only. The hosts come from pages a run read, so they are attacker-influenced: every
request, redirect hop and `<link>` target is checked with `fetch.public`, and what comes back is
trusted for nothing but its first bytes. The content type served is the one those bytes show, and
SVG is refused outright because it would run script when served from this origin.
"""

from __future__ import annotations

import logging
import re
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from kvasir.config import FaviconSettings
from kvasir.research import fetch
from kvasir.research.pages import BUSY_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# An icon past this is not one, or not worth the disk.
MAX_BYTES = 256 * 1024
# Of a homepage, only the head matters, and sites that put it past this are not worth reading on.
HOMEPAGE_BYTES = 512 * 1024
ATTEMPT_SECONDS = 5.0
# A whole lookup, across providers. Past it no further provider is tried.
DEADLINE_SECONDS = 10.0
MAX_REDIRECTS = 3
MAX_CANDIDATES = 3
SECONDS_PER_DAY = 86400

# Lookups run here, not on the caller's thread, because name resolution has no timeout: a request
# stops waiting at the deadline, and a slow name holds a worker rather than an API thread. Not
# `runtime.host_slot`: its interval per host would pace every provider lookup, and a research run's
# fetch permits would starve these.
_workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="favicon")
# Past this many lookups in progress, a new one is refused rather than queued without end.
MAX_FLIGHTS = 64

_TEMPLATES = {
    "duckduckgo": "https://icons.duckduckgo.com/ip3/{domain}.ico",
    "google": "https://www.google.com/s2/favicons?domain={domain}&sz=64",
}
_REDIRECTS = (301, 302, 303, 307, 308)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SIZE = re.compile(r"(\d+)x(\d+)")
_ICON_RELS = {"icon", "apple-touch-icon", "apple-touch-icon-precomposed"}


def normalize_host(raw: str) -> str | None:
    """`raw` as the key a site is cached under: lowercase, no `www.` and no port. None for an IP
    literal or anything that is not a DNS name with a dot."""
    host = raw.strip().lower()
    if host.count(":") > 1 or host.startswith("["):
        return None
    host, _, port = host.partition(":")
    if port and not port.isdigit():
        return None
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    host = host.removeprefix("www.")
    labels = host.split(".")
    # A name never ends in digits only, which also keeps out 1.2.3.4 and 127.1.
    if len(host) > 253 or len(labels) < 2 or labels[-1].isdigit():
        return None
    return host if all(_LABEL.fullmatch(label) for label in labels) else None


class Icon(NamedTuple):
    body: bytes
    content_type: str


class Found(NamedTuple):
    """What a lookup came to. `final` is False when it could not tell: the deadline cut it short, or
    every request failed in transport, so asking again later may find an icon."""

    icon: Icon | None
    final: bool


class Entry(NamedTuple):
    """A stored answer: the icon, or None for a site that has none."""

    icon: Icon | None
    provider: str | None
    fetched_at: float


class FaviconStore:
    """One table in one file. Each operation opens its own connection, so any thread can call."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            # A NULL body is a site found to have no icon, remembered so it is not asked again.
            db.execute(
                "CREATE TABLE IF NOT EXISTS favicons ("
                "host TEXT PRIMARY KEY, body BLOB, content_type TEXT, provider TEXT, "
                "fetched_at REAL NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, host: str, ttl_days: int, miss_ttl_days: int) -> Entry | None:
        """The answer stored for `host`, unless it is older than `ttl_days` (an icon) or
        `miss_ttl_days` (no icon)."""
        with self._connect() as db:
            row = db.execute(
                "SELECT body, content_type, provider, fetched_at FROM favicons WHERE host = ?",
                (host,),
            ).fetchone()
        if row is None:
            return None
        body, content_type, provider, fetched_at = row
        if (
            time.time() - fetched_at
            >= (ttl_days if body is not None else miss_ttl_days) * SECONDS_PER_DAY
        ):
            return None
        return Entry(None if body is None else Icon(body, content_type), provider, fetched_at)

    def put(self, host: str, icon: Icon | None, provider: str | None) -> None:
        """Replaces the answer for `host`; no `icon` records that the site has none."""
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO favicons VALUES (?, ?, ?, ?, ?)",
                (
                    host,
                    icon and icon.body,
                    icon and icon.content_type,
                    provider,
                    time.time(),
                ),
            )

    def flush(self) -> None:
        """Drops every icon, then returns the space to the disk."""
        with self._connect() as db:
            db.execute("DELETE FROM favicons")
        # VACUUM refuses to run in a transaction, so it gets a connection that opens none.
        with closing(sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)) as db:
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


@dataclass
class _Flight:
    """One lookup in progress, which others for the same host wait on."""

    done: threading.Event = field(default_factory=threading.Event)
    found: Found = Found(None, False)


@dataclass
class _Budget:
    """The clock of one lookup, and whether anything answered it. A transport error is no answer."""

    deadline: float
    heard: bool = False
    unresolved: bool = False


class Favicons:
    """Icons by host: from the store while fresh, otherwise from the providers in order."""

    def __init__(self, settings: FaviconSettings, store: FaviconStore) -> None:
        self._settings = settings
        self._store = store
        self._flights: dict[str, _Flight] = {}
        self._lock = threading.Lock()

    def find(self, host: str) -> Found:
        """The icon of `host`, or None when it has none, is not a public site, or favicons are off,
        and whether a None is the final answer. Never raises for a failed request or store."""
        key = normalize_host(host)
        if not self._settings.providers or key is None:
            return Found(None, True)
        with self._lock:
            flight = self._flights.get(key)
            if flight is None:
                if len(self._flights) >= MAX_FLIGHTS:
                    return Found(None, False)
                flight = self._flights[key] = _Flight()
                _workers.submit(self._fly, key, flight)
        # One that outlasts this wait still finishes, and what it finds is stored for the next ask.
        flight.done.wait(DEADLINE_SECONDS)
        return flight.found

    def flush(self) -> None:
        self._store.flush()

    def _fly(self, host: str, flight: _Flight) -> None:
        try:
            flight.found = self._resolve(host)
        except Exception:
            logger.exception("favicon lookup failed for %s", host)
        finally:
            # Removed so the table holds only lookups in progress.
            with self._lock:
                del self._flights[host]
            flight.done.set()

    def _resolve(self, host: str) -> Found:
        settings = self._settings
        try:
            entry = self._store.get(host, settings.ttl_days, settings.miss_ttl_days)
        except sqlite3.Error as exc:
            logger.warning("favicon cache unreadable, fetching %s: %s", host, exc)
            entry = None
        if entry:
            return Found(entry.icon, True)
        budget = _Budget(time.monotonic() + DEADLINE_SECONDS)
        icon, provider = None, None
        for provider in settings.providers:
            if time.monotonic() >= budget.deadline:
                break
            icon = self._ask(provider, host, budget)
            if icon:
                break
        # A miss is remembered only when every provider had its turn and one of them answered. One
        # cut short by the deadline might have answered, and slowness or an outage is not an answer.
        if icon is None and (time.monotonic() >= budget.deadline or not budget.heard):
            return Found(None, False)
        try:
            self._store.put(host, icon, provider if icon else None)
        except sqlite3.Error as exc:
            logger.warning("favicon cache not written for %s: %s", host, exc)
        return Found(icon, True)

    def _ask(self, provider: str, host: str, budget: _Budget) -> Icon | None:
        if provider == "direct":
            return _direct(host, budget)
        template = self._settings.custom_url if provider == "custom" else _TEMPLATES[provider]
        # `custom` is set by the operator and may be a service on their own network, so its first
        # request skips the public check. A redirect from it is still checked.
        return _icon(template.replace("{domain}", host), budget, trusted=provider == "custom")


def _direct(host: str, budget: _Budget) -> Icon | None:
    icon = _icon(f"https://{host}/favicon.ico", budget)
    if icon:
        return icon
    # Resolving the name again for the homepage would only wait as long a second time.
    if budget.unresolved:
        return None
    home = f"https://{host}/"
    # Its robots.txt may cost a wait of its own, which the deadline is checked against after.
    if not fetch.allowed(home) or time.monotonic() >= budget.deadline:
        return None
    page = _get(home, budget, limit=HOMEPAGE_BYTES, truncate=True)
    if page is None:
        return None
    for url in _icon_links(*page)[:MAX_CANDIDATES]:
        icon = _icon(url, budget)
        if icon:
            return icon
    return None


def _icon(url: str, budget: _Budget, trusted: bool = False) -> Icon | None:
    got = _get(url, budget, trusted=trusted)
    if got is None:
        return None
    content_type = _sniff(got[0])
    return Icon(got[0], content_type) if content_type else None


def _sniff(body: bytes) -> str | None:
    """The type of an image format that is safe to serve, from its first bytes alone."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    # An ICO has a count of images, and a CUR has type 2.
    if body.startswith(b"\x00\x00\x01\x00") and len(body) >= 6 and body[4:6] != b"\x00\x00":
        return "image/x-icon"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def _get(
    url: str,
    budget: _Budget,
    *,
    trusted: bool = False,
    limit: int = MAX_BYTES,
    truncate: bool = False,
) -> tuple[bytes, str] | None:
    """The body and final URL of a 2xx answer, or None. Past `limit` bytes it is None, or with
    `truncate` the first `limit`. Redirects are followed by hand."""
    if time.monotonic() >= budget.deadline:
        return None
    try:
        return _follow(
            url,
            min(budget.deadline, time.monotonic() + ATTEMPT_SECONDS),
            budget,
            trusted,
            limit,
            truncate,
        )
    except httpx.HTTPError as exc:
        logger.warning("favicon request failed for %s: %s", url, exc)
        return None
    # urlsplit refuses a URL such as a redirect to a bad IPv6 host. That is an answer, of a kind.
    except (httpx.InvalidURL, ValueError) as exc:
        budget.heard = True
        logger.warning("favicon request failed for %s: %s", url, exc)
        return None


def _follow(
    url: str, give_up: float, budget: _Budget, trusted: bool, limit: int, truncate: bool
) -> tuple[bytes, str] | None:
    for hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            budget.heard = True
            return None
        if not (trusted and hop == 0) and not fetch.public(parts.hostname):
            # A name that did not resolve may be an outage, which is not an answer.
            budget.unresolved = not _resolves(parts.hostname)
            budget.heard = budget.heard or not budget.unresolved
            return None
        remaining = give_up - time.monotonic()
        if remaining <= 0:
            return None
        with httpx.stream(
            "GET",
            url,
            timeout=remaining,
            follow_redirects=False,
            # Compressed, a small read could decode to far more than the limit before it is checked.
            headers={"User-Agent": fetch.user_agent(), "Accept-Encoding": "identity"},
        ) as response:
            budget.heard = True
            location = response.headers.get("location")
            if response.status_code in _REDIRECTS and location:
                url = urljoin(url, location)
                continue
            # A placeholder is served with the 404 by some providers, so the body does not count.
            if not response.is_success:
                return None
            if response.headers.get("content-encoding", "identity") != "identity":
                return None
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                # An image cut short would pass the sniff and be kept as if whole.
                if len(body) > limit or time.monotonic() > give_up:
                    if not truncate:
                        return None
                    break
            return bytes(body[:limit]), url
    return None


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    return True


def _icon_links(html: bytes, page_url: str) -> list[str]:
    """The icons a page declares as absolute URLs, the largest declared first."""
    soup = BeautifulSoup(html, "html.parser")
    base = soup.find("base", href=True)
    base_url = urljoin(page_url, str(base["href"])) if isinstance(base, Tag) else page_url
    found: dict[str, int] = {}
    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or ""
        tokens = {t.lower() for t in ([rel] if isinstance(rel, str) else rel)}
        if not tokens & _ICON_RELS:
            continue
        try:
            url = urljoin(base_url, str(link["href"]).strip())
            parts = urlsplit(url)
        except ValueError:
            continue
        kind = str(link.get("type") or "").strip().lower()
        if parts.scheme not in ("http", "https") or kind == "image/svg+xml":
            continue
        if parts.path.lower().endswith(".svg"):
            continue
        sizes = [int(w) * int(h) for w, h in _SIZE.findall(str(link.get("sizes") or "").lower())]
        found.setdefault(url, max(sizes, default=0))
    # Stable, so equally sized icons stay in the page's order.
    return sorted(found, key=lambda url: -found[url])
