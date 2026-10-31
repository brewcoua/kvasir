"""Page download and text extraction.

A page is cut into pieces small enough that one model call sees one piece, the notes and the
question, and nothing else.
"""

from __future__ import annotations

import io
import re

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from kvasir.storm import runtime

TIMEOUT_SECONDS = 20.0
# A page past this is a dump rather than an article, and parsing it costs more than it gives.
MAX_CHARACTERS = 1_000_000
# A download stops here. A PDF cut short does not parse, and falls back to its snippet like any
# other unreadable page.
MAX_BYTES = 20_000_000
# About 1,500 tokens: room for the notes, the instructions and the answer in a 16k context.
PIECE_CHARACTERS = 6000
# Some sites refuse a request that names no browser.
USER_AGENT = "Mozilla/5.0 (compatible; kvasir)"

_BOILERPLATE = ["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]
# A word the PDF broke across two lines, as in "compu-\ntation". A compound broken at its own
# hyphen loses that hyphen, which is the rarer case in justified text.
_LINE_BREAK_HYPHEN = re.compile(r"(\w)-[ \t]*\n[ \t]*([a-z])")


class FetchError(Exception):
    """The page could not be read as text."""


def fetch_text(url: str) -> str:
    """The readable text of an HTML page or a PDF. Other formats are refused, not parsed."""
    try:
        with (
            runtime.fetch_slot(),
            httpx.stream(
                "GET",
                url,
                timeout=TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as response,
        ):
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            # Many servers send a PDF as a generic download, so those are read and sniffed.
            readable = ("html", "pdf", "octet-stream")
            if content_type and not any(kind in content_type for kind in readable):
                raise FetchError(f"{url}: not HTML or PDF ({content_type})")
            body = _read(response)
    except httpx.HTTPError as exc:
        raise FetchError(f"{url}: {exc}") from exc
    if "html" in content_type:
        return extract(body.decode(response.encoding or "utf-8", errors="replace")[:MAX_CHARACTERS])
    if body.startswith(b"%PDF-"):
        return extract_pdf(url, body)
    raise FetchError(f"{url}: not HTML or PDF ({content_type or 'no content type'})")


def _read(response: httpx.Response) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) >= MAX_BYTES:
            break
    return bytes(body[:MAX_BYTES])


def extract(html: str) -> str:
    """Visible text, one block per line, without navigation and other page furniture."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_BOILERPLATE):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


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
