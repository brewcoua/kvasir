"""Page download and text extraction.

A page is cut into pieces small enough that one model call sees one piece, the notes and the
question, and nothing else.
"""

from __future__ import annotations

import httpx
from bs4 import BeautifulSoup

from kvasir.storm import runtime

TIMEOUT_SECONDS = 20.0
# A page past this is a dump rather than an article, and parsing it costs more than it gives.
MAX_CHARACTERS = 1_000_000
# About 1,500 tokens: room for the notes, the instructions and the answer in a 16k context.
PIECE_CHARACTERS = 6000
# Some sites refuse a request that names no browser.
USER_AGENT = "Mozilla/5.0 (compatible; kvasir)"

_BOILERPLATE = ["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]


class FetchError(Exception):
    """The page could not be read as text."""


def fetch_text(url: str) -> str:
    """The readable text of an HTML page. PDFs and other formats are refused, not parsed."""
    try:
        with runtime.fetch_slot():
            response = httpx.get(
                url,
                timeout=TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FetchError(f"{url}: {exc}") from exc
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type:
        raise FetchError(f"{url}: not HTML ({content_type or 'no content type'})")
    return extract(response.text[:MAX_CHARACTERS])


def extract(html: str) -> str:
    """Visible text, one block per line, without navigation and other page furniture."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_BOILERPLATE):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def split(text: str, size: int = PIECE_CHARACTERS) -> list[str]:
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
