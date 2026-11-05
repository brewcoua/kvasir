"""What kind of site a host is, and how far to trust it: government, news, AI-generated and so on.

A host resolves in order: config `allow`/`deny`, a mark, the lists, suffix rules, the cache, then
Wikidata. With `rsp`, Wikipedia's perennial sources list adds `rsp_*` tags to what the host
resolves to, and sets no kind. Hosts come from search results, so they are attacker-influenced: they
are normalized before they reach a query, and the only text sent to Wikidata is a host or a URL
built from one.
Nothing here is built unless `[sources] enabled`; the caller decides that.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import tomllib
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel
from tld import get_fld

from kvasir.config import SourceSettings
from kvasir.research import fetch
from kvasir.research.favicons import normalize_host
from kvasir.research.pages import BUSY_TIMEOUT_SECONDS
from kvasir.schemas.results import SourceInfo, Tier

logger = logging.getLogger(__name__)

R = TypeVar("R", bound=BaseModel)

# Past this a host still unresolved is `unknown` for the pick, and known by the next.
DEADLINE_SECONDS = 5.0
QUERY_TIMEOUT_SECONDS = 30.0
LIST_TIMEOUT_SECONDS = 30.0
# The CISA list is about 1 MB; past this a list is not one, or not worth the disk.
MAX_LIST_BYTES = 16 * 1024 * 1024
# Every page the perennial sources list transcludes, in one answer: the rows are in its subpages.
RSP_URL = (
    "https://en.wikipedia.org/w/api.php?action=query&generator=templates"
    "&titles=Wikipedia:Reliable_sources/Perennial_sources&gtlnamespace=4&gtllimit=max"
    "&prop=revisions&rvprop=content&rvslots=main&formatversion=2&format=json"
)
# After a transport error or a 5xx, so a down endpoint is not asked again by every search.
FAILURE_BACKOFF_SECONDS = 10.0
MAX_BACKOFF_SECONDS = 3600.0
BATCH_SIZE = 50
# WDQS answers a 429 with Retry-After; without one, this long.
DEFAULT_BACKOFF_SECONDS = 60.0
# How often the downloaded lists are checked for age.
LIST_CHECK_SECONDS = 3600.0
SECONDS_PER_DAY = 86400
# Past this many hosts in flight, a new one is left unknown rather than queued without end.
MAX_FLIGHTS = 256

# Lookups run here so a slow query holds a worker, not the research run.
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sources")

# In priority order: a host that several items classify takes the first.
KINDS = (
    "intergovernmental",
    "government",
    "academic",
    "standards",
    "news",
    "reference",
    "organization",
    "unknown",
)
_HIGH_KINDS = frozenset(KINDS[:4])
_AVOID_TAGS = frozenset({"ai_generated", "content_farm", "predatory", "rsp_deprecated", "deny"})
# The tags a list or the bundled file may set.
LIST_TAGS = frozenset({"ai_generated", "content_farm", "predatory"})
# A row's class on the perennial sources page, and its tag. The least reliable comes last.
_RSP_STATUS = {
    "gr": "rsp_reliable",
    "nc": "rsp_mixed",
    "gu": "rsp_unreliable",
    "d": "rsp_deprecated",
    "b": "rsp_deprecated",
}
_RSP_ORDER = ("rsp_reliable", "rsp_mixed", "rsp_unreliable", "rsp_deprecated")
MARKS = frozenset({"allow", "deny", *KINDS[:-1]})
# What a list may be labelled: a kind, or a tag.
LIST_LABELS = frozenset({*KINDS[:-1], *LIST_TAGS})

_NS = "http://www.wikidata.org/entity/"
_ROOT_KINDS = {
    "Q327333": "government",
    "Q2659904": "government",
    # A country: `canada.ca` is the Government of Canada's site, and its item is the country's.
    "Q6256": "government",
    "Q3918": "academic",
    "Q31855": "academic",
    "Q4671277": "academic",
    "Q1328899": "standards",
    "Q11032": "news",
    "Q192283": "news",
    "Q1153191": "news",
    "Q5292": "reference",
    "Q43229": "organization",
}
_INTERNATIONAL, _NON_GOVERNMENTAL = "Q484652", "Q79913"
_ROOT_TAGS = {"Q171": "community", "Q125361038": "ai_generated"}
_ROOTS = " ".join(f"wd:{q}" for q in (*_ROOT_KINDS, *_ROOT_TAGS, _INTERNATIONAL, _NON_GOVERNMENTAL))
_QUERY = """SELECT ?h ?i ?l ?root WHERE {{
  VALUES ?root {{ {roots} }}
  {{ VALUES (?h ?d) {{ {names} }} ?i wdt:P13337 ?d }}
  UNION
  {{ VALUES (?h ?u) {{ {urls} }} ?i wdt:P856 ?u }}
  ?i wdt:P31/wdt:P279* ?root .
  OPTIONAL {{ ?i rdfs:label ?l FILTER(LANG(?l) = "en") }}
}} GROUP BY ?h ?i ?l ?root"""


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """What is known of one host. `tags` are `official`, `community`, `ai_generated`,
    `content_farm`, `predatory`, `rsp_*`, and `allow` or `deny` from the config or a mark."""

    # The host as asked, lowercase and without `www.`.
    host: str
    kind: str
    tags: frozenset[str] = frozenset()
    label: str = ""
    qid: str = ""
    # config, mark, list, rule or wikidata; empty when nothing is known.
    origin: str = ""

    @property
    def tier(self) -> Tier:
        return tier(self.kind, self.tags)


def tier(kind: str, tags: Iterable[str] = ()) -> Tier:
    """`avoid`, `low`, `high`, `normal` or `unknown`, by the first rule that fits."""
    tags = frozenset(tags)
    if tags & _AVOID_TAGS:
        return "avoid"
    if "rsp_unreliable" in tags:
        return "low"
    if kind in _HIGH_KINDS or tags & {"rsp_reliable", "allow"}:
        return "high"
    return "unknown" if kind == "unknown" else "normal"


class _Match(NamedTuple):
    """What Wikidata said of a host."""

    kind: str
    tags: frozenset[str]
    label: str
    qid: str


class Entry(NamedTuple):
    """A stored answer: the match, or None for a host Wikidata does not know."""

    match: _Match | None


class SourceStore:
    """Two tables in one file: `sources`, a cache that expires, and `marks`, which never do. Each
    operation opens its own connection, so any thread can call."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            # A NULL kind is a host Wikidata does not know, remembered so it is not asked again.
            db.execute(
                "CREATE TABLE IF NOT EXISTS sources ("
                "host TEXT PRIMARY KEY, kind TEXT, tags TEXT, label TEXT, qid TEXT, "
                "fetched_at REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS marks ("
                "host TEXT PRIMARY KEY, value TEXT NOT NULL, marked_at REAL NOT NULL)"
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
        """The answer stored for `host`, unless it is older than `ttl_days` (a match) or
        `miss_ttl_days` (no match)."""
        with self._connect() as db:
            row = db.execute(
                "SELECT kind, tags, label, qid, fetched_at FROM sources WHERE host = ?", (host,)
            ).fetchone()
        if row is None:
            return None
        kind, tags, label, qid, fetched_at = row
        if time.time() - fetched_at >= (ttl_days if kind else miss_ttl_days) * SECONDS_PER_DAY:
            return None
        if kind is None:
            return Entry(None)
        return Entry(_Match(kind, frozenset(tags.split(",")) - {""}, label, qid))

    def put(self, host: str, match: _Match | None) -> None:
        """Replaces the answer for `host`; no `match` records that Wikidata does not know it."""
        fields: tuple[str | None, ...] = (None,) * 4
        if match:
            fields = (match.kind, ",".join(sorted(match.tags)), match.label, match.qid)
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?)",
                (host, *fields, time.time()),
            )

    def clear(self) -> None:
        """Forgets every cached answer, and keeps the marks."""
        with self._connect() as db:
            db.execute("DELETE FROM sources")

    def mark(self, host: str, value: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO marks VALUES (?, ?, ?)", (host, value, time.time()))

    def unmark(self, host: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM marks WHERE host = ?", (host,))

    def marks(self, hosts: list[str]) -> dict[str, str]:
        """The marks set on any of `hosts`, by host."""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT host, value FROM marks WHERE host IN ({','.join('?' * len(hosts))})",
                hosts,
            ).fetchall()
        return dict(rows)


class Sources:
    """Sources by host: from the config, marks, lists and rules at once, otherwise from the cache
    or Wikidata."""

    def __init__(self, settings: SourceSettings, store: SourceStore, data_dir: Path) -> None:
        self._settings = settings
        self._store = store
        self._data_dir = data_dir
        self._allow = _hosts(settings.allow)
        self._deny = _hosts(settings.deny)
        self._flights: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._backoff_until = 0.0
        self._bundled = _bundled()
        self._index: dict[str, Source] | None = None
        self._rsp: dict[str, frozenset[str]] = {}
        self._lists_checked = float("-inf")

    def classify(
        self, hosts: Iterable[str], deadline: float = DEADLINE_SECONDS
    ) -> dict[str, Source]:
        """Each of `hosts` as a Source. What the config, marks, lists, rules and cache know is
        returned at once; the rest wait at most `deadline` seconds for Wikidata, then are
        `unknown`, and are known by the next call. Never raises for a failed request or store."""
        self._check_lists()
        out: dict[str, Source] = {}
        asked: dict[str, str] = {}
        for raw in hosts:
            key = normalize(raw)
            if key is None:
                out[raw] = Source(host=raw, kind="unknown")
            else:
                asked[raw] = key
        marks = self._marks(list(asked.values()))
        waiting: list[str] = []
        fetching: list[str] = []
        for raw, key in asked.items():
            fixed = self._fixed(key, marks)
            if fixed is not None:
                out[raw] = self._overlay(fixed)
                continue
            rule = _rule(key)
            entry = self._cached(key)
            if entry is None and self._settings.wikidata:
                fetching.append(key)
                if rule is None:
                    waiting.append(raw)
            out[raw] = self._overlay(_merge(key, rule, entry and entry.match))
        flights = self._start(fetching)
        end = time.monotonic() + deadline
        for raw in waiting:
            flight = flights.get(asked[raw])
            if flight:
                flight.wait(max(0.0, end - time.monotonic()))
            # Read again even with no flight: a lookup that finished since the first read stored it.
            entry = self._cached(asked[raw])
            out[raw] = self._overlay(_merge(asked[raw], None, entry and entry.match))
        return out

    def mark(self, host: str, value: str) -> None:
        """Sets `allow`, `deny` or a kind on `host`, until `unmark`."""
        key = normalize(host)
        if key is None or value not in MARKS:
            raise ValueError(f"cannot mark {host!r} as {value!r}")
        self._store.mark(key, value)

    def unmark(self, host: str) -> None:
        key = normalize(host)
        if key is not None:
            self._store.unmark(key)

    def locked(self, host: str) -> bool:
        """Whether the config's `allow` or `deny` decides `host`, so a mark cannot."""
        key = normalize(host)
        return key is not None and any(n in self._allow or n in self._deny for n in _chain(key))

    def marked(self, host: str) -> bool:
        key = normalize(host)
        return key is not None and key in self._store.marks([key])

    def flush(self) -> None:
        """Forgets the cached Wikidata answers; marks and the config stay."""
        self._store.clear()

    def refresh_lists(self) -> None:
        """Downloads each list, and with `rsp` the perennial sources page, that is missing or older
        than `ttl_days`, then rebuilds the index. One that fails to download keeps the copy on
        disk."""
        self._lists_checked = time.monotonic()
        for entry in self._list_entries():
            label, _, url = entry.partition("=")
            path = self._list_path(url)
            if path.exists() and time.time() - path.stat().st_mtime < (
                self._settings.ttl_days * SECONDS_PER_DAY
            ):
                continue
            try:
                body = bytearray()
                with httpx.stream(
                    "GET",
                    url,
                    headers={"User-Agent": fetch.user_agent()},
                    timeout=LIST_TIMEOUT_SECONDS,
                    follow_redirects=True,
                ) as response:
                    if not response.is_success:
                        raise httpx.HTTPError(f"status {response.status_code}")
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > MAX_LIST_BYTES:
                            raise ValueError(f"longer than {MAX_LIST_BYTES} bytes")
                self._data_dir.mkdir(parents=True, exist_ok=True)
                # Renamed into place so a reader never sees half a list.
                temp = path.with_suffix(".tmp")
                temp.write_bytes(body)
                os.replace(temp, path)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                logger.warning("source list %s not fetched (%s): %s", label, url, exc)
        self._index = self._load_lists()
        self._rsp = self._load_rsp()

    def _list_entries(self) -> tuple[str, ...]:
        if self._settings.enabled and self._settings.rsp:
            return (*self._settings.lists, f"rsp={RSP_URL}")
        return self._settings.lists

    def _check_lists(self) -> None:
        if not self._list_entries():
            return
        with self._lock:
            if self._index is None:
                self._index = self._load_lists()
                self._rsp = self._load_rsp()
            if time.monotonic() - self._lists_checked < LIST_CHECK_SECONDS:
                return
            self._lists_checked = time.monotonic()
        # Its own thread: a slow download must not hold a Wikidata lookup's worker.
        threading.Thread(target=self.refresh_lists, name="sources-lists", daemon=True).start()

    def _list_path(self, url: str) -> Path:
        return self._data_dir / f"sources-{hashlib.sha1(url.encode()).hexdigest()[:12]}.txt"

    def _load_lists(self) -> dict[str, Source]:
        index: dict[str, Source] = {}
        for entry in self._settings.lists:
            label, _, url = entry.partition("=")
            if label in LIST_TAGS:
                template = Source(host="", kind="unknown", tags=frozenset({label}), origin="list")
            elif label in KINDS[:-1]:
                template = Source(host="", kind=label, origin="list")
            else:
                logger.warning("source list %r ignored: its label is not a kind or tag", entry)
                continue
            try:
                text = self._list_path(url).read_text(encoding="utf-8")
            except (OSError, ValueError) as exc:
                logger.warning("source list %s unreadable: %s", label, exc)
                continue
            for host in parse_list(text):
                index[host] = template
        return index

    def _load_rsp(self) -> dict[str, frozenset[str]]:
        if not (self._settings.enabled and self._settings.rsp):
            return {}
        try:
            text = self._list_path(RSP_URL).read_text(encoding="utf-8")
            return {host: frozenset({tag}) for host, tag in parse_rsp(text).items()}
        except (OSError, ValueError, LookupError, TypeError) as exc:
            logger.warning("perennial sources list unreadable: %s", exc)
            return {}

    def _overlay(self, source: Source) -> Source:
        """`source` with the `rsp_*` tags of its host or registrable domain. A config entry or a
        mark decides alone."""
        if source.origin in ("config", "mark"):
            return source
        for name in dict.fromkeys((source.host, _registrable(source.host))):
            if name in self._rsp:
                return replace(
                    source, tags=source.tags | self._rsp[name], origin=source.origin or "list"
                )
        return source

    def _marks(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        names = list({name for key in keys for name in _chain(key)})
        try:
            return self._store.marks(names)
        except sqlite3.Error as exc:
            logger.warning("source marks unreadable: %s", exc)
            return {}

    def _fixed(self, key: str, marks: dict[str, str]) -> Source | None:
        """The source of `key` when the config, a mark or a list decides it."""
        chain = _chain(key)
        if any(name in self._deny for name in chain):
            return Source(host=key, kind="unknown", tags=frozenset({"deny"}), origin="config")
        if any(name in self._allow for name in chain):
            return Source(host=key, kind="unknown", tags=frozenset({"allow"}), origin="config")
        for name in chain:
            if name in marks:
                value = marks[name]
                if value in ("allow", "deny"):
                    return Source(host=key, kind="unknown", tags=frozenset({value}), origin="mark")
                return Source(host=key, kind=value, origin="mark")
        domain = _registrable(key)
        index = {**(self._index or {}), **self._bundled}
        for name in dict.fromkeys((key, domain)):
            if name in index:
                found = index[name]
                return Source(host=key, kind=found.kind, tags=found.tags, origin="list")
        return None

    def _cached(self, key: str) -> Entry | None:
        if not self._settings.wikidata:
            return None
        try:
            return self._store.get(key, self._settings.ttl_days, self._settings.miss_ttl_days)
        except sqlite3.Error as exc:
            logger.warning("source cache unreadable for %s: %s", key, exc)
            return None

    def _start(self, keys: list[str]) -> dict[str, threading.Event]:
        """The flight of each of `keys`, starting batches for those with none."""
        flights: dict[str, threading.Event] = {}
        new: dict[str, threading.Event] = {}
        with self._lock:
            for key in dict.fromkeys(keys):
                flight = self._flights.get(key)
                # A flight that ended after the caller's read has stored its answer by now.
                if (
                    flight is None
                    and self._cached(key) is None
                    and len(self._flights) < MAX_FLIGHTS
                ):
                    flight = self._flights[key] = new[key] = threading.Event()
                if flight is not None:
                    flights[key] = flight
        names = list(new)
        for start in range(0, len(names), BATCH_SIZE):
            batch = {name: new[name] for name in names[start : start + BATCH_SIZE]}
            _workers.submit(self._fly, batch)
        return flights

    def _fly(self, batch: dict[str, threading.Event]) -> None:
        try:
            self._lookup(list(batch))
        except Exception:
            logger.exception("source lookup failed for %d hosts", len(batch))
        finally:
            with self._lock:
                for key in batch:
                    del self._flights[key]
            for flight in batch.values():
                flight.set()

    def _lookup(self, hosts: list[str]) -> None:
        if time.monotonic() < self._backoff_until:
            return
        matches = self._ask([(host, host) for host in hosts])
        if matches is None:
            return
        # The registrable domain is tried only for a host whose own name matched nothing, so a
        # subdomain with an item of its own does not take its parent's.
        parents = [(host, _registrable(host)) for host in hosts if host not in matches]
        parents = [(host, name) for host, name in parents if name != host]
        if parents:
            more = self._ask(parents)
            if more is None:
                skipped = {host for host, _ in parents}
                hosts = [host for host in hosts if host not in skipped]
            else:
                matches |= more
        for host in hosts:
            try:
                self._store.put(host, matches.get(host))
            except sqlite3.Error as exc:
                logger.warning("source cache not written for %s: %s", host, exc)

    def _ask(self, pairs: list[tuple[str, str]]) -> dict[str, _Match] | None:
        """The matches for each (host, name) of `pairs`, or None when the query failed, in which
        case the endpoint is left alone for a while."""
        try:
            response = httpx.post(
                self._settings.endpoint,
                data={"query": _query(pairs)},
                headers={
                    "User-Agent": fetch.user_agent(),
                    "Accept": "application/sparql-results+json",
                },
                timeout=QUERY_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            logger.warning("wikidata query failed: %s", exc)
            self._back_off(FAILURE_BACKOFF_SECONDS)
            return None
        if response.status_code == 429:
            try:
                wait = float(response.headers.get("retry-after", ""))
            except ValueError:
                wait = math.nan
            if not math.isfinite(wait):
                wait = DEFAULT_BACKOFF_SECONDS
            wait = min(max(wait, 0.0), MAX_BACKOFF_SECONDS)
            logger.warning("wikidata rate limit hit, backing off %.0f s", wait)
            self._back_off(wait)
            return None
        if not response.is_success:
            logger.warning("wikidata query answered %d", response.status_code)
            self._back_off(FAILURE_BACKOFF_SECONDS)
            return None
        try:
            return _parse(response.json())
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("wikidata answer unreadable: %s", exc)
            return None

    def _back_off(self, seconds: float) -> None:
        self._backoff_until = time.monotonic() + seconds


def info(source: Source) -> SourceInfo:
    return SourceInfo(
        kind=source.kind,
        tags=sorted(source.tags),
        tier=source.tier,
        label=source.label,
        qid=source.qid,
        origin=source.origin,
    )


def describe(
    sources: Sources | None, hosts: Iterable[str], deadline: float = DEADLINE_SECONDS
) -> dict[str, SourceInfo]:
    """Each of `hosts` as a SourceInfo, or nothing when `sources` is None (the feature is off)."""
    if sources is None:
        return {}
    return {host: info(source) for host, source in sources.classify(hosts, deadline).items()}


def describe_urls(sources: Sources | None, urls: Iterable[str]) -> dict[str, SourceInfo]:
    """Each of `urls` as the SourceInfo of its site, from what is known at once: nothing waits for
    Wikidata, and a lookup the cache lacks finishes in the background. Empty when `sources` is
    None."""
    urls = list(urls)
    hosts = {url: urlparse(url).hostname or "" for url in urls}
    found = describe(sources, set(hosts.values()), deadline=0)
    return {url: found[host] for url, host in hosts.items() if host in found}


def with_sources(sources: Sources | None, result: R) -> R:
    """`result` with its citations' `source` filled as they are now, so a mark applies to a run
    already stored. Citations are stored without one."""
    citations = getattr(result, "citations", None)
    if sources is None or not citations:
        return result
    found = describe_urls(sources, (citation.url for citation in citations))
    return result.model_copy(
        update={
            "citations": [
                citation.model_copy(update={"source": found.get(citation.url)})
                for citation in citations
            ]
        }
    )


def parse_list(text: str) -> set[str]:
    """The hosts in a hosts file, a uBlacklist (`*://*.example.com/*`) or a CSV whose first column
    is the host. Comments, regexes and rules for part of a site are skipped."""
    hosts: set[str] = set()
    for line in text.splitlines():
        # A comment after the host is cut; a hosts file has the address first and the host second.
        line = re.split(r"\s#", line.strip())[0].split(",")[0].strip()
        if not line or line[0] in "#!/":
            continue
        words = line.split()
        token = words[min(1, len(words) - 1)].removeprefix("*://").removeprefix("*.")
        name, slash, path = token.partition("/")
        if slash and path not in ("", "*"):
            continue
        host = normalize(name)
        if host:
            hosts.add(host)
    return hosts


def parse_rsp(text: str) -> dict[str, str]:
    """The `rsp_*` tag of each site on the perennial sources page, from the JSON the API answers
    with. A row is `|- class="s-<status>"` and its sites are in `{{WP:RSPUSES|a.com|b.com}}`. A
    site in several rows (a topic with a status of its own) takes the least reliable. Rows with no
    site, a status that is none of the five, or a part of a site (`a.com/news`) are skipped."""
    found: dict[str, str] = {}
    for page in json.loads(text)["query"]["pages"]:
        wikitext = page["revisions"][0]["slots"]["main"]["content"] if "revisions" in page else ""
        for status, row in re.findall(
            r'^\|- class="s-(\w+)"(.*?)(?=^\|-|\Z)', wikitext, re.M | re.S
        ):
            tag = _RSP_STATUS.get(status)
            for names in re.findall(r"\{\{WP:RSPUSES\|([^{}]*)\}\}", row):
                for raw in names.split("|"):
                    name, slash, path = raw.partition("/")
                    host = normalize(name)
                    if not tag or not host or (slash and path not in ("", "*")):
                        continue
                    if host not in found or _RSP_ORDER.index(tag) > _RSP_ORDER.index(found[host]):
                        found[host] = tag
    return found


def normalize(raw: str) -> str | None:
    """`normalize_host` of `raw`, which may end in the dot of a fully qualified name."""
    return normalize_host(raw.strip().removesuffix("."))


def _hosts(names: Iterable[str]) -> frozenset[str]:
    keys = {name: normalize(name) for name in names}
    for name in [name for name, key in keys.items() if key is None]:
        logger.warning("source host %r is not a DNS name, ignored", name)
    return frozenset(key for key in keys.values() if key)


def _bundled() -> dict[str, Source]:
    data = tomllib.loads(Path(__file__).with_name("sources.toml").read_text(encoding="utf-8"))
    return {
        host: Source(host="", kind="unknown", tags=frozenset({tag}), origin="list")
        for host, tag in data.items()
    }


def _registrable(host: str) -> str:
    return get_fld(host, fail_silently=True, fix_protocol=True) or host


def _chain(host: str) -> list[str]:
    """`host` and its parents down to two labels, most specific first."""
    labels = host.split(".")
    return [".".join(labels[i:]) for i in range(len(labels) - 1)]


def _rule(host: str) -> str | None:
    """The kind the host's suffix gives, which Wikidata cannot override."""
    labels = host.split(".")
    if labels[-1] == "int" or host == "europa.eu" or host.endswith(".europa.eu"):
        return "intergovernmental"
    country = len(labels[-1]) == 2 and len(labels) > 1
    if (
        labels[-1] in ("gov", "mil")
        or (country and labels[-2] in ("gov", "gouv"))
        or host.endswith(".gc.ca")
    ):
        return "government"
    if labels[-1] == "edu" or (country and labels[-2] in ("ac", "edu")):
        return "academic"
    return None


def _merge(host: str, rule: str | None, match: _Match | None) -> Source:
    kind = rule or (match.kind if match else "unknown")
    origin = "rule" if rule else "wikidata" if match else ""
    if match is None:
        return Source(host=host, kind=kind, origin=origin)
    return Source(
        host=host, kind=kind, tags=match.tags, label=match.label, qid=match.qid, origin=origin
    )


def _query(pairs: list[tuple[str, str]]) -> str:
    names, urls = [], []
    for host, name in pairs:
        names.append(f'("{host}" "{name}")')
        for scheme in ("http", "https"):
            for www in ("", "www."):
                for slash in ("", "/"):
                    urls.append(f'("{host}" <{scheme}://{www}{name}{slash}>)')
    return _QUERY.format(roots=_ROOTS, names=" ".join(names), urls=" ".join(urls))


def _parse(answer: dict[str, dict[str, list[dict[str, dict[str, str]]]]]) -> dict[str, _Match]:
    """The matches in a WDQS JSON answer, by host. A host takes the best kind across its items."""
    roots: dict[str, dict[str, set[str]]] = {}
    labels: dict[str, str] = {}
    for row in answer["results"]["bindings"]:
        host, item = row["h"]["value"], row["i"]["value"].removeprefix(_NS)
        roots.setdefault(host, {}).setdefault(item, set()).add(
            row["root"]["value"].removeprefix(_NS)
        )
        if "l" in row:
            labels[item] = row["l"]["value"]
    out: dict[str, _Match] = {}
    for host, items in roots.items():
        best = ("unknown", "")
        tags = {"official"}
        for item, reached in items.items():
            kinds = {_ROOT_KINDS[q] for q in reached if q in _ROOT_KINDS}
            if _INTERNATIONAL in reached and _NON_GOVERNMENTAL not in reached:
                kinds.add("intergovernmental")
            tags |= {_ROOT_TAGS[q] for q in reached if q in _ROOT_TAGS}
            for kind in kinds:
                if KINDS.index(kind) < KINDS.index(best[0]):
                    best = (kind, item)
        out[host] = _Match(best[0], frozenset(tags), labels.get(best[1], ""), best[1])
    return out
