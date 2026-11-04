"""Page download and text extraction.

A page is cut into pieces small enough that one model call sees one piece, the notes and the
question, and nothing else.
"""

from __future__ import annotations

import io
import ipaddress
import logging
import re
import socket
import sqlite3
import threading
import time
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import courlan
import httpx
import trafilatura
from bs4 import BeautifulSoup, Tag
from pypdf import PdfReader

from kvasir import __version__
from kvasir.storm import runtime

if TYPE_CHECKING:
    from kvasir.research.pages import PageStore

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20.0
# A page past this is a dump rather than an article, and parsing it costs more than it gives.
MAX_CHARACTERS = 1_000_000
# A download stops here. A PDF cut short does not parse, and falls back to its snippet like any
# other unreadable page.
MAX_BYTES = 20_000_000
# About 1,500 tokens: room for the notes, the instructions and the answer in a 16k context.
PIECE_CHARACTERS = 6000
# A Retry-After longer than this is not waited out: a run would sit on one page. Wikimedia's policy
# asks for a pause of this length when a 429 names none.
MAX_RETRY_AFTER_SECONDS = 30.0
DEFAULT_RETRY_AFTER_SECONDS = 5.0
# Joins paragraphs, and sections, in a piece: the blank line trafilatura puts between blocks.
SEPARATOR = "\n\n"
# trafilatura returning less than this has missed the article, as on a page of short blocks, and
# the cleaned page's text is used instead. Untuned.
MIN_TEXT = 200

_BOILERPLATE = ["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]
# Interactive elements, from web-som's SELECTORS, are controls and never prose. Links are kept
# because their text is read, and [tabindex] and [contenteditable] also mark content.
_INTERACTIVE = [
    "button",
    'input:not([type="hidden"])',
    "select",
    "textarea",
    ".btn",
    '[role="button"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="input"]',
    '[role="menuitem"]',
    '[role="menuitemcheckbox"]',
    '[role="menuitemradio"]',
    '[role="option"]',
    '[role="switch"]',
    '[role="tab"]',
    '[role="treeitem"]',
    '[role="gridcell"]',
    '[role="search"]',
    '[role="combobox"]',
    '[role="listbox"]',
    '[role="slider"]',
    '[role="spinbutton"]',
]
# Firecrawl's excludeNonMainTags, used when trafilatura finds too little. Not applied before it:
# trafilatura already drops the cookie banner and share box of the saved pages.
_NON_MAIN = [
    "header",
    "footer",
    "nav",
    "aside",
    ".header",
    ".top",
    ".navbar",
    "#header",
    ".footer",
    ".bottom",
    "#footer",
    ".sidebar",
    ".side",
    ".aside",
    "#sidebar",
    ".modal",
    ".popup",
    "#modal",
    ".overlay",
    ".ad",
    ".ads",
    ".advert",
    "#ad",
    ".lang-selector",
    ".language",
    "#language-selector",
    ".social",
    ".social-media",
    ".social-links",
    "#social",
    ".menu",
    ".navigation",
    "#nav",
    ".breadcrumbs",
    "#breadcrumbs",
    ".share",
    "#share",
    ".widget",
    "#widget",
    ".cookie",
    "#cookie",
    '[aria-label="Cookie consent" i]',
]
# Firecrawl's forceIncludeMainTags, the one that applies outside its customers' own sites. An
# element holding it is kept whatever its class says, as a "#main" under a ".top" wrapper.
_FORCE_MAIN = "#main"
# A footnote marker as trafilatura leaves it, "<sup>\[12\]</sup>", and the wiki edit links. Not any
# bracket pair: a page's own "[1, 2]" or a transcription such as "[ˈkwɑsez̠]" is content.
_FOOTNOTE = re.compile(
    r"<sup>(?:\\\[[^\]\\]{1,8}\\\])+</sup>|\\\[(?:edit|citation needed)\\\]", re.IGNORECASE
)
# A word the PDF broke across two lines, as in "compu-\ntation". A compound broken at its own
# hyphen loses that hyphen, which is the rarer case in justified text.
_LINE_BREAK_HYPHEN = re.compile(r"(\w)-[ \t]*\n[ \t]*([a-z])")
# A longer "heading" is a paragraph set in a heading tag. Repeated at the top of pieces it would
# crowd out their text.
MAX_HEADING = 200
# A markdown heading line, as trafilatura writes one.
_HEADING = re.compile(r"(#{1,6}) \S")
# Crawl4AI's tier 1 markers: only a block or challenge page holds them, so they count at any size.
_CHALLENGE = re.compile(
    r"Reference\s*#\s*\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+|Pardon\s+Our\s+Interruption"
    r"|challenge-form.*?__cf_chl_f_tk=|<span\s+class=\"cf-error-code\">\d{4}</span>"
    r"|/cdn-cgi/challenge-platform/\S+orchestrate|window\._pxAppId\s*=|captcha\.px-cdn\.net"
    r"|captcha-delivery\.com|_Incapsula_Resource|Incapsula\s+incident\s+ID"
    r"|Sucuri\s+WebSite\s+Firewall|KPSDK\.scriptStart\s*=\s*KPSDK\.now\(\)"
    r"|blocked\s+by\s+network\s+security",
    re.IGNORECASE | re.DOTALL,
)
# Tier 2: phrases that also occur in articles and login forms, so only a short page is judged by
# them.
_BLOCK_PHRASE = re.compile(
    r"Access\s+Denied|Checking\s+your\s+browser|<title>\s*Just\s+a\s+moment"
    r"|class=[\"'](?:g-recaptcha|h-captcha)[\"']|Access\s+to\s+This\s+Page\s+Has\s+Been\s+Blocked"
    r"|blocked\s+by\s+security|Request\s+unsuccessful",
    re.IGNORECASE,
)
MAX_BLOCK_PHRASE_BYTES = 10_000
# The titles FlareSolverr looks for to know it is facing a challenge.
_BLOCK_TITLE = re.compile(
    r"<title>\s*(?:Just a moment\.\.\.|DDoS-Guard|Access denied|Attention Required! \| Cloudflare)",
    re.IGNORECASE,
)
# Crawl4AI's tier 3: a page this small with two signs of having no content is an empty shell, one
# sign is enough when it is smaller still.
MAX_SHELL_BYTES = 50_000
MAX_SHELL_ONE_SIGN_BYTES = 5_000
_BODY = re.compile(r"<body\b", re.IGNORECASE)
_CONTENT_ELEMENT = re.compile(r"<(?:p|h[1-6]|article|section|li|td|a|pre)\b", re.IGNORECASE)
_SCRIPT = re.compile(r"<script\b", re.IGNORECASE)
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
# A route of a single-page app names a page, so courlan, which drops every fragment, must not see
# it.
_ROUTE = re.compile(r"#!?/.*")
# Where a link sits, from its ancestors' tags and from the class or id they carry.
_NAV_MARK = re.compile(r"menu|navbar|navigation|breadcrumb", re.IGNORECASE)
_FOOTER_MARK = re.compile(r"footer", re.IGNORECASE)
_RELATED_MARK = re.compile(r"related|see-also|further-reading|recommended", re.IGNORECASE)
# A section a link is under, however its box is marked. Matched at the start of the heading.
_RELATED_HEADINGS = (
    "see also",
    "related",
    "further reading",
    "external links",
    "you may also like",
)
# Characters of the block around a link that are kept, centred on its anchor.
CONTEXT_CHARACTERS = 200
SECONDS_PER_HOUR = 3600

_cache: tuple[PageStore, int] | None = None
# This run's own max age, which a run sets for itself; the pools copy context, so it reaches them.
_max_age: ContextVar[int | None] = ContextVar("max_age_hours", default=None)
# Per origin, what its robots.txt allows: the rules, or whether everything is.
# With the time each was read. RFC 9309 asks for robots.txt to be read again after 24 hours.
_robots: dict[str, tuple[RobotFileParser | bool, float]] = {}
ROBOTS_MAX_AGE_SECONDS = 24 * 3600
_robots_lock = threading.Lock()


@dataclass(frozen=True)
class Link:
    """A link of a page: the normalized target, its anchor text, the text around it, and where on
    the page it sits."""

    url: str
    anchor: str
    context: str
    region: Literal["main", "related", "nav", "footer"]


Engine = Literal["static", "render", "stealth"]
# The headings above a section and its own, and the text.
_Section = tuple[list[tuple[int, str]], str]


@dataclass(frozen=True)
class Page:
    """A page read: its text, and the links found before any of the page was dropped."""

    url: str
    final_url: str
    title: str
    text: str
    engine: Engine
    links: list[Link]
    # Unix seconds when the page was read from its site, which a cached page keeps.
    fetched_at: float


class FetchError(Exception):
    """The page could not be read as text."""


# What a challenge or a block answers with, before any page of its own: worth a browser's try.
_BLOCKED = (403, 503)


class UnusablePage(FetchError):
    """A static page that holds no article: `reason` is "challenge" for a block or challenge page,
    "blocked" for a 403 or 503, "shell" for a page whose content a browser would have to build."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _user_agent() -> str:
    """Names the service, where to read about it and who runs it, as Wikimedia's policy asks."""
    contact = f"; {runtime.fetch_contact()}" if runtime.fetch_contact() else ""
    site = f"+https://github.com/brewcoua/kvasir{contact}"
    return f"kvasir/{__version__} ({site}) httpx/{httpx.__version__}"


def _retry_after(value: str | None) -> float:
    """Seconds a Retry-After header asks for, as delta-seconds or an HTTP-date."""
    if value is None:
        return DEFAULT_RETRY_AFTER_SECONDS
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS
    # A date with no zone is GMT, as HTTP dates are.
    return max(0.0, (when.replace(tzinfo=when.tzinfo or UTC) - datetime.now(UTC)).total_seconds())


def check_page(url: str, html: str) -> None:
    """Raises UnusablePage when static `html` is a challenge, a block page or an empty shell."""
    # Block pages bury their text under inline scripts and styles, so those are cut for the scan.
    bare = _SCRIPT_OR_STYLE.sub("", html[:500_000])[:30_000]
    found = _CHALLENGE.search(html[:15_000]) or _CHALLENGE.search(bare) or _BLOCK_TITLE.search(html)
    if not found and len(html) < MAX_BLOCK_PHRASE_BYTES:
        found = _BLOCK_PHRASE.search(html)
    if found:
        raise UnusablePage(
            f"{url}: challenge or block page ({found.group().strip()[:40]})", "challenge"
        )
    if len(html) > MAX_SHELL_BYTES:
        return
    visible = len(_TAG.sub("", _SCRIPT_OR_STYLE.sub("", html)).strip())
    content = len(_CONTENT_ELEMENT.findall(html))
    signs = [
        *(["no body"] if not _BODY.search(html) else []),
        *(["little text"] if visible < 50 else []),
        *(["no content elements"] if content == 0 else []),
        *(["scripts only"] if content == 0 and visible < 100 and _SCRIPT.search(html) else []),
    ]
    if "no body" in signs or len(signs) >= 2 or (signs and len(html) < MAX_SHELL_ONE_SIGN_BYTES):
        raise UnusablePage(f"{url}: empty page shell ({', '.join(signs)})", "shell")


def configure_cache(store: PageStore | None, max_age_hours: int) -> None:
    """Serve pages from `store` while they are under `max_age_hours` old, and keep what is read.
    None, which is the default, caches nothing."""
    global _cache
    _cache = None if store is None else (store, max_age_hours)


def cache() -> PageStore | None:
    """The store `configure_cache` set, or None."""
    return None if _cache is None else _cache[0]


def use_max_age(hours: int | None) -> None:
    """Serve cached pages under `hours` old, in this context only. None uses the configured age."""
    _max_age.set(hours)


def fetch_page(url: str) -> Page:
    """An HTML page or a PDF, as text and links. Other formats are refused, not parsed.

    With a cache configured, a fresh copy is returned without a request, a stale one is
    revalidated, and a page read from its site replaces the copy. A failure leaves the copy alone.
    """
    if _cache is None:
        downloaded = _download(url, {})
        assert downloaded
        return downloaded[0]
    store, max_age_hours = _cache
    run_max_age = _max_age.get()
    if run_max_age is not None:
        max_age_hours = run_max_age
    # The cache saves downloads; a cache that fails, a disk full or a lock held too long, costs
    # them but must not cost the run.
    try:
        # 0 means fetch again, whatever the copy says.
        cached = store.get(url) if max_age_hours else None
    except sqlite3.Error as exc:
        logger.warning("page cache unreadable, fetching %s: %s", url, exc)
        cached = None
    headers: dict[str, str] = {}
    if cached:
        page, etag, last_modified = cached
        if time.time() - page.fetched_at < max_age_hours * SECONDS_PER_HOUR:
            return page
        headers = {
            **({"If-None-Match": etag} if etag else {}),
            **({"If-Modified-Since": last_modified} if last_modified else {}),
        }
    downloaded = _download(url, headers)
    if downloaded is None:
        assert cached
        try:
            return replace(cached[0], fetched_at=store.touch(url))
        except sqlite3.Error as exc:
            logger.warning("page cache not refreshed for %s: %s", url, exc)
            return cached[0]
    try:
        store.put(*downloaded)
    except sqlite3.Error as exc:
        logger.warning("page cache not written for %s: %s", url, exc)
    return downloaded[0]


def allowed(url: str) -> bool:
    """Whether a lead may be read: its host is public, and the robots.txt of its site lets this
    crawler read it. No robots.txt, or a 4xx, allows all (RFC 9309). A 5xx or no answer allows none:
    a lead costs little to skip."""
    parts = urlsplit(url)
    # A lead's URL is written by the page it was on, so it must not reach this service's network.
    if not _public(parts.hostname or ""):
        return False
    origin = f"{parts.scheme}://{parts.netloc}"
    with _robots_lock:
        cached = _robots.get(origin)
    if cached is None or time.time() - cached[1] >= ROBOTS_MAX_AGE_SECONDS:
        cached = (_robots_txt(origin, parts.hostname or ""), time.time())
        with _robots_lock:
            _robots[origin] = cached
    rules = cached[0]
    return rules if isinstance(rules, bool) else rules.can_fetch("kvasir", url)


def _public(host: str) -> bool:
    """Whether every address `host` resolves to is on the public internet."""
    try:
        addresses = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    # The zone of a link-local IPv6 address, as in fe80::1%eth0, is not part of the address.
    return all(ipaddress.ip_address(str(info[4][0]).split("%")[0]).is_global for info in addresses)


def _robots_txt(origin: str, host: str) -> RobotFileParser | bool:
    try:
        with runtime.host_slot(host):
            response = httpx.get(
                f"{origin}/robots.txt",
                timeout=TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": _user_agent()},
            )
    except httpx.HTTPError:
        return False
    if 400 <= response.status_code < 500:
        return True
    if not response.is_success:
        return False
    rules = RobotFileParser()
    rules.parse(response.text[:MAX_CHARACTERS].splitlines())
    return rules


def _download(url: str, validators: dict[str, str]) -> tuple[Page, str | None, str | None] | None:
    """The page, with the ETag and Last-Modified its site sent, or None when `validators` were
    sent and the site answered 304."""
    # The host asked is the one in the URL; a redirect to another is not counted against that one.
    host = urlsplit(url).hostname or ""
    retried = False
    while True:
        try:
            with (
                runtime.host_slot(host),
                httpx.stream(
                    "GET",
                    url,
                    timeout=TIMEOUT_SECONDS,
                    follow_redirects=True,
                    headers={"User-Agent": _user_agent(), **validators},
                ) as response,
            ):
                if response.status_code == 429:
                    delay = _retry_after(response.headers.get("retry-after"))
                    if retried or delay > MAX_RETRY_AFTER_SECONDS:
                        raise FetchError(f"{url}: rate limited, retry after {delay:g}s")
                    # The retry waits at the top of the loop, after this slot is released.
                    runtime.delay_host(host, delay)
                    retried = True
                    continue
                if response.status_code == 304 and validators:
                    return None
                if response.status_code in _BLOCKED and runtime.fetch_render() != "off":
                    blocked = UnusablePage(f"{url}: {response.status_code}", "blocked")
                    return _rendered(url, blocked), None, None
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                # Many servers send a PDF as a generic download, so those are read and sniffed.
                readable = ("html", "pdf", "octet-stream")
                if content_type and not any(kind in content_type for kind in readable):
                    raise FetchError(f"{url}: not HTML or PDF ({content_type})")
                body = _read(response)
            break
        except httpx.HTTPError as exc:
            raise FetchError(f"{url}: {exc}") from exc
    if "html" in content_type:
        html = body.decode(response.encoding or "utf-8", errors="replace")[:MAX_CHARACTERS]
        try:
            check_page(url, html)
        except UnusablePage as exc:
            if runtime.fetch_render() == "off":
                raise
            return _rendered(url, exc), None, None
        page = _html_page(url, str(response.url), html, "static")
    elif body.startswith(b"%PDF-"):
        page = Page(url, str(response.url), "", extract_pdf(url, body), "static", [], time.time())
    else:
        raise FetchError(f"{url}: not HTML or PDF ({content_type or 'no content type'})")
    return page, response.headers.get("etag"), response.headers.get("last-modified")


def _rendered(url: str, static: UnusablePage) -> Page:
    """The page as a browser builds it, for one whose static HTML was a shell, a challenge or a
    block. Without a browser to build it, the static page's failure stands."""
    # Imported here: `browser` imports this module, and a run without the extra never needs it.
    from kvasir.research.browser import RenderUnavailable, render

    try:
        rendered = render(url)
    except RenderUnavailable:
        raise static from None
    if rendered.status >= 400:
        raise FetchError(f"{url}: {rendered.status} when rendered")
    check_page(url, rendered.html)
    engine: Engine = "stealth" if runtime.fetch_stealth() else "render"
    return _html_page(url, rendered.final_url, rendered.html[:MAX_CHARACTERS], engine)


def _html_page(url: str, final_url: str, html: str, engine: Engine) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    # Read first: extraction drops tags in place.
    title, links = _title(soup), _links(soup, final_url)
    return Page(url, final_url, title, _extract(soup), engine, links, time.time())


def _read(response: httpx.Response) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) >= MAX_BYTES:
            break
    return bytes(body[:MAX_BYTES])


def extract(html: str) -> str:
    """The article as markdown, without navigation and other page furniture."""
    return _extract(BeautifulSoup(html, "html.parser"))


def _extract(soup: BeautifulSoup) -> str:
    for tag in soup.select(", ".join(_INTERACTIVE)):
        tag.decompose()
    text = trafilatura.extract(
        str(soup),
        output_format="markdown",
        include_tables=True,
        include_comments=False,
        include_links=False,
        include_images=False,
        favor_recall=True,
    )
    if text and len(text) >= MIN_TEXT:
        return _FOOTNOTE.sub("", text)
    for tag in soup(_BOILERPLATE):
        tag.decompose()
    for selector in _NON_MAIN:
        for tag in soup.select(selector):
            if not tag.decomposed and not tag.select_one(_FORCE_MAIN):
                tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def normalize(url: str) -> str:
    """`url` in the form two spellings of one page share: https, no `www.`, no fragment, tracking
    parameters, trailing slash or `index.html`. A `#/` or `#!/` route is kept."""
    route = _ROUTE.search(url)
    clean = courlan.normalize_url(url[: route.start()] if route else url, strict=True)
    parts = urlsplit(clean)
    path = parts.path.removesuffix("index.html").rstrip("/")
    netloc = parts.netloc.removeprefix("www.")
    return urlunsplit(("https", netloc, path, parts.query, "")) + (route.group() if route else "")


def _title(soup: BeautifulSoup) -> str:
    tag = soup.title if soup.title and soup.title.get_text().strip() else soup.h1
    return " ".join(tag.get_text().split()) if tag else ""


def _links(soup: BeautifulSoup, final_url: str) -> list[Link]:
    """The page's links, one per target: the first outside `nav` and `footer`, else the first."""
    base = soup.find("base", href=True)
    base_url = urljoin(final_url, str(base["href"])) if isinstance(base, Tag) else final_url
    own = normalize(final_url)
    found: dict[str, Link] = {}
    for a in soup.select("a[href]"):
        try:
            target = urljoin(base_url, str(a["href"]).strip())
            if urlsplit(target).scheme not in ("http", "https") or normalize(target) == own:
                continue
            url = normalize(target)
        # courlan and urljoin refuse a URL they cannot parse, such as a bad IPv6 host.
        except ValueError:
            continue
        region = _region(a)
        if url not in found or (
            found[url].region in ("nav", "footer") and region not in ("nav", "footer")
        ):
            # Kept as written: a site may answer only under its www. or http spelling.
            found[url] = Link(target, " ".join(a.get_text().split()), _context(a), region)
    return list(found.values())


def _region(a: Tag) -> Literal["main", "related", "nav", "footer"]:
    regions: set[str] = set()
    for tag in a.parents:
        # The root's classes are feature flags, as Wikipedia's "main-menu-disabled", not a region.
        if tag.name in ("html", "body", "[document]"):
            continue
        marks = f"{tag.get('id', '')} {' '.join(tag.get('class') or [])}"
        role = tag.get("role")
        if tag.name in ("nav", "header") or role == "navigation" or _NAV_MARK.search(marks):
            regions.add("nav")
        if tag.name == "footer" or role == "contentinfo" or _FOOTER_MARK.search(marks):
            regions.add("footer")
        if tag.name == "aside" or _RELATED_MARK.search(marks):
            regions.add("related")
    heading = a.find_previous(["h2", "h3", "h4"])
    if heading and " ".join(heading.get_text().split()).lower().startswith(_RELATED_HEADINGS):
        regions.add("related")
    for region in ("nav", "footer", "related"):
        if region in regions:
            return region
    return "main"


def _context(a: Tag) -> str:
    block = a.find_parent(["p", "li", "td", "dd", "figcaption"]) or a.parent or a
    text = " ".join(block.get_text().split())
    if len(text) <= CONTEXT_CHARACTERS:
        return text
    anchor = " ".join(a.get_text().split())
    middle = max(text.find(anchor), 0) + len(anchor) // 2
    start = min(max(middle - CONTEXT_CHARACTERS // 2, 0), len(text) - CONTEXT_CHARACTERS)
    return text[start : start + CONTEXT_CHARACTERS]


def extract_pdf(url: str, data: bytes) -> str:
    """The text layer of a PDF, read page by page until there is enough of it."""
    pages: list[str] = []
    length = 0
    try:
        for page in PdfReader(io.BytesIO(data)).pages:
            if length >= MAX_CHARACTERS:
                break
            text = page.extract_text()
            pages.append(text)
            length += len(text)
    # The file comes from anywhere, and a malformed one fails with whatever the parser hit first,
    # not only pypdf's own errors. Any of them means the page is unreadable, not that the run is.
    except Exception as exc:
        raise FetchError(f"{url}: unreadable PDF ({exc})") from exc
    text = _LINE_BREAK_HYPHEN.sub(r"\1\2", "\n".join(pages))[:MAX_CHARACTERS]
    lines = (line.strip() for line in text.splitlines())
    text = "\n".join(line for line in lines if line)
    if not text:
        raise FetchError(f"{url}: PDF has no text layer")
    return text


def split(text: str, size: int = PIECE_CHARACTERS) -> list[str]:
    """Pieces of at most `size` characters, cut at the page's headings where it has any.

    Sections are merged while they fit, never across a top-level heading: the shallowest level
    that occurs more than once, so a page's lone title does not count. A section over `size` is
    cut between paragraphs. Each piece begins with the headings above it that it does not start
    with, as the page wrote them, so a quote from it stays on the page.
    """
    sections = _sections(text)
    if not any(path for path, _ in sections):
        return _lines(text, size)
    levels = Counter(path[-1][0] for path, _ in sections if path)
    top = min((level for level, count in levels.items() if count > 1), default=0)
    pieces: list[str] = []
    block: list[_Section] = []
    for section in sections:
        path = section[0]
        if block and path and path[-1][0] == top:
            pieces += _merge(block, size, top)
            block = []
        block.append(section)
    return pieces + _merge(block, size, top)


def _sections(text: str) -> list[_Section]:
    """The text cut before each heading line, with the headings above each section and its own."""
    sections: list[tuple[list[tuple[int, str]], list[str]]] = [([], [])]
    path: list[tuple[int, str]] = []
    fenced = False
    for line in text.splitlines():
        # A "# comment" in a code block is not a heading.
        fenced ^= line.startswith("```")
        heading = None if fenced or len(line) > MAX_HEADING else _HEADING.match(line)
        if heading:
            level = len(heading[1])
            path = [*(h for h in path if h[0] < level), (level, line)]
            sections.append((path, []))
        sections[-1][1].append(line)
    return [(p, "\n".join(lines).strip("\n")) for p, lines in sections if "".join(lines).strip()]


def _heading_lines(path: list[tuple[int, str]], top: int) -> str:
    # Above the top level is the page's title, which the reader is already given.
    return "".join(f"{line}\n" for level, line in path if level >= top)


def _merge(sections: list[_Section], size: int, top: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for path, body in sections:
        ancestors = _heading_lines(path[:-1], top)
        whole = _heading_lines(path, top)
        if len(ancestors) + len(body) <= size:
            parts = [(ancestors, body)]
        else:
            cut = _join(_paragraphs(body, size - len(whole)), size - len(whole))
            parts = [(ancestors, cut[0]), *((whole, part) for part in cut[1:])]
        for lead, part in parts:
            if current and len(current) + len(SEPARATOR) + len(part) <= size:
                current += SEPARATOR + part
            else:
                if current:
                    pieces.append(current)
                current = lead + part
    return [*pieces, current] if current else pieces


def _paragraphs(text: str, size: int) -> list[str]:
    return [piece for paragraph in text.split(SEPARATOR) for piece in _lines(paragraph, size)]


def _join(parts: list[str], size: int) -> list[str]:
    joined: list[str] = []
    for part in parts:
        if joined and len(joined[-1]) + len(SEPARATOR) + len(part) <= size:
            joined[-1] += SEPARATOR + part
        else:
            joined.append(part)
    return joined


def _lines(text: str, size: int) -> list[str]:
    """Pieces of at most `size` characters, cut between lines where a line allows it."""
    pieces: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > size:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:size])
            line = line[size:]
        if current and len(current) + 1 + len(line) > size:
            pieces.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        pieces.append(current)
    return pieces
