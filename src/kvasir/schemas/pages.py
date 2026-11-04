"""The page cache as the knowledge page shows it."""

from __future__ import annotations

from pydantic import BaseModel


class Site(BaseModel):
    """One domain with pages cached."""

    domain: str
    pages: int
    pieces: int
    # Unix seconds of the latest fetch among its pages.
    fetched_at: float
    engines: list[str]


class PageSummary(BaseModel):
    """One cached page, without its text."""

    id: str
    url: str
    title: str
    engine: str
    fetched_at: float
    pieces: int
    # Characters of the extracted text.
    length: int


class PageLead(BaseModel):
    """A link of a page, as kept for a run to follow."""

    url: str
    anchor: str
    context: str


class PageRun(BaseModel):
    """A stored run whose result cites the page."""

    id: str
    topic: str


class PageDetail(BaseModel):
    """One cached page with the text intake reads."""

    id: str
    url: str
    final_url: str
    title: str
    engine: str
    fetched_at: float
    # Markdown, as extracted.
    text: str
    # By region of the page: main, related, nav or footer.
    leads: dict[str, list[PageLead]]
    runs: list[PageRun]


class RecallPiece(BaseModel):
    """A cached piece scored against a question."""

    page: str
    url: str
    title: str
    site: str
    text: str
    similarity: float
    # Whether it reaches RECALL_MIN.
    clears: bool


class Recall(BaseModel):
    """The pieces recall would take for a question, best first, with the threshold it applies."""

    minimum: float
    pieces: list[RecallPiece]


class CacheStats(BaseModel):
    """What the page cache holds and what it takes on disk."""

    pages: int
    links: int
    pieces: int
    # Bytes of page text, and of piece vectors.
    text_bytes: int
    vector_bytes: int
    # The database file with its -wal and -shm files.
    file_bytes: int
    path: str
    # Unix seconds of the oldest and newest fetch; None when empty.
    oldest: float | None
    newest: float | None


class SiteNode(BaseModel):
    """One cached site in the link graph."""

    domain: str
    pages: int
    # Links stored on its pages, wherever they lead.
    links: int


class SiteEdge(BaseModel):
    """Links from the pages of one cached site to the pages of another."""

    source: str
    target: str
    count: int


class SiteGraph(BaseModel):
    """The cached sites and the links between them."""

    sites: list[SiteNode]
    edges: list[SiteEdge]
