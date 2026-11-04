"""Page rendering in a headless browser, for pages whose text a script builds.

Playwright's async API is bound to one event loop and is not safe across threads, while the
pipeline fetches from a thread pool. So one daemon thread runs an event loop that owns the browser,
and `render` submits to it and waits.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import importlib
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from kvasir.research.fetch import _INTERACTIVE, FetchError
from kvasir.storm import runtime

LOAD_TIMEOUT_MS = 15_000
# Pages that keep a connection open never go idle, so the wait for it is short and a timeout is
# not an error.
IDLE_TIMEOUT_MS = 2_000
# The loop thread is waited on for longer than a page's own timeouts add up to; past this the
# render is cancelled.
RENDER_TIMEOUT_SECONDS = 45.0
# Pages open in the browser at once, whatever the number of hosts.
MAX_PAGES = 2
# Hosts whose context, and so whose cookies, the process keeps; the oldest is closed past this. A
# context holds its own cache and storage, so one per host ever seen would grow without end.
MAX_CONTEXTS = 16
# From web-som: an element covering more of the viewport than this is a wall or an overlay, not
# content.
MAX_COVER_RATIO = 0.8
_BLOCKED_RESOURCES = {"image", "media", "font"}
# Firecrawl's AD_SERVING_DOMAINS (apps/playwright-service-ts/api.ts), matched as a substring of the
# host as it does.
_AD_HOSTS = (
    "doubleclick.net",
    "adservice.google.com",
    "googlesyndication.com",
    "googletagservices.com",
    "googletagmanager.com",
    "google-analytics.com",
    "adsystem.com",
    "adservice.com",
    "adnxs.com",
    "ads-twitter.com",
    "facebook.net",
    "fbcdn.net",
    "amazon-adsystem.com",
)
# It removes what a reader would not read, in one pass over the page. Hidden by
# style, zero size, aria-hidden, a fixed or sticky element covering most of the viewport, and
# controls. web-som's occlusion test (is another element drawn over this one's sample points) is
# left out: it calls elementFromPoint for a grid of points per element, which on a long page costs
# seconds, and the fixed-element rule already catches the overlays that matter.
_CLEAN = """
(args) => {
  const [selectors, maxCover] = args;
  const area = innerWidth * innerHeight;
  const drop = new Set(document.body.querySelectorAll(selectors.join(",")));
  for (const el of document.body.querySelectorAll("*")) {
    const style = getComputedStyle(el);
    if (style.display === "contents" || el.tagName === "BR" || el.tagName === "WBR") continue;
    const rect = el.getBoundingClientRect();
    // Only a leaf is dropped for its size: a wrapper with no height may still hold floats.
    const empty = el.childElementCount === 0 && (rect.width === 0 || rect.height === 0);
    const covers = (style.position === "fixed" || style.position === "sticky")
      && rect.width * rect.height > maxCover * area;
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0"
        || empty || covers || el.getAttribute("aria-hidden") === "true") drop.add(el);
  }
  for (const el of drop) el.remove();
  return document.documentElement.outerHTML;
}
"""


class RenderUnavailable(FetchError):
    """No browser to render with: the package or the Chromium binary is not installed."""


@dataclass(frozen=True, slots=True)
class Rendered:
    html: str
    final_url: str
    status: int


_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
# Set once the browser could not start, so later calls fail without starting it again.
_unavailable: str | None = None
# The rest belongs to the loop thread.
_setup = asyncio.Lock()
_pages = asyncio.Semaphore(MAX_PAGES)
_api: Any = None
_playwright: Any = None
_browser: Any = None
_contexts: dict[str, Any] = {}


def render(url: str) -> Rendered:
    """The page after its scripts have run, with what a reader would not read removed."""
    if _unavailable:
        raise RenderUnavailable(_unavailable)
    host = urlsplit(url).hostname or ""
    with runtime.host_slot(host):
        future = asyncio.run_coroutine_threadsafe(_render(url, host), _start_loop())
        try:
            return future.result(RENDER_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            future.cancel()
            raise FetchError(f"{url}: render timed out") from exc
        except FetchError:
            raise
        # Whatever the browser hit, a navigation error, a crashed page, is the page's failure and
        # not the run's.
        except Exception as exc:
            raise FetchError(f"{url}: {exc}") from exc


def _start_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="kvasir-browser", daemon=True).start()
            atexit.register(_close)
            _loop = loop
        return _loop


async def _context(host: str) -> Any:
    """The host's context, kept for the process so its cookies persist. Starts the browser."""
    global _api, _playwright, _browser, _unavailable
    async with _setup:
        if _browser is None:
            name = "patchright" if runtime.fetch_stealth() else "playwright"
            try:
                _api = importlib.import_module(f"{name}.async_api")
                _playwright = await _api.async_playwright().start()
                _browser = await _playwright.chromium.launch()
            except Exception as exc:
                # A missing package, or a Chromium that `playwright install` has not fetched.
                _unavailable = f"cannot render with {name}: {exc}"
                raise RenderUnavailable(_unavailable) from exc
        if host not in _contexts:
            if len(_contexts) >= MAX_CONTEXTS:
                await _contexts.pop(next(iter(_contexts))).close()
            context = await _browser.new_context(service_workers="block")
            await context.route("**/*", _filter)
            _contexts[host] = context
        return _contexts[host]


async def _filter(route: Any) -> None:
    request = route.request
    host = urlsplit(request.url).hostname or ""
    if request.resource_type in _BLOCKED_RESOURCES or any(ad in host for ad in _AD_HOSTS):
        await route.abort()
    else:
        await route.continue_()


async def _render(url: str, host: str) -> Rendered:
    context = await _context(host)
    async with _pages:
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="load", timeout=LOAD_TIMEOUT_MS)
            with contextlib.suppress(_api.TimeoutError):
                await page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
            html = await page.evaluate(_CLEAN, [_INTERACTIVE, MAX_COVER_RATIO])
            return Rendered(html, page.url, response.status if response else 0)
        finally:
            await page.close()


async def _shutdown() -> None:
    if _browser is not None:
        await _browser.close()
        await _playwright.stop()


def _close() -> None:
    if _loop is None or _browser is None:
        return
    # Exiting: a browser that will not close dies with its parent.
    with contextlib.suppress(Exception):
        asyncio.run_coroutine_threadsafe(_shutdown(), _loop).result(10)
