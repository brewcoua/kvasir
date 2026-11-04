"""Rendering in a headless browser. Only the unavailable case runs without Chromium."""

from __future__ import annotations

import http.server
import sys
import threading

import pytest

from kvasir.research import browser
from kvasir.research.fetch import FetchError
from kvasir.storm import runtime

PAGE = b"""<!DOCTYPE html><html><head><title>t</title></head><body>
<div id="root"></div>
<div class="wall" style="position:fixed;inset:0;background:#000"><p>We value your privacy</p>
<button>Accept</button></div>
<button>Subscribe</button>
<p style="display:none">hidden words</p>
<script>
  document.getElementById("root").innerHTML = "<p>Rendered by script</p>";
</script></body></html>"""


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    runtime.configure_fetch("", 0)
    monkeypatch.setattr(browser, "_unavailable", None)
    yield
    runtime.configure_fetch("", runtime.DEFAULT_HOST_INTERVAL_SECONDS)


def test_render_is_unavailable_without_playwright(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    monkeypatch.setattr(browser, "_browser", None)

    with pytest.raises(browser.RenderUnavailable, match="playwright"):
        browser.render("http://render.example/")
    # Once known, it fails without starting anything.
    assert browser._unavailable
    with pytest.raises(FetchError):
        browser.render("http://render.example/")


@pytest.mark.browser
def test_render_runs_scripts_and_drops_overlays_and_controls():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        rendered = browser.render(url)
    finally:
        server.shutdown()

    assert rendered.status == 200
    assert rendered.final_url == url
    assert "Rendered by script" in rendered.html
    for gone in ("We value your privacy", "Accept", "Subscribe", "hidden words"):
        assert gone not in rendered.html
