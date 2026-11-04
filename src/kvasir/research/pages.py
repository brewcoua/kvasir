"""Pages kept in SQLite across runs, so a page read once is not downloaded again while it is fresh.

Public page content only: nothing here carries what a user asked.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Collection, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import NamedTuple, cast

import numpy as np

from kvasir.research.corroborate import domain
from kvasir.research.fetch import Engine, Link, Page, normalize

# Writers from several runs wait for each other rather than failing.
BUSY_TIMEOUT_SECONDS = 30.0


def page_id(key: str) -> str:
    """The id of the page cached under the normalized URL `key`: stateless and safe in a URL."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _host(url: str) -> str:
    """The site of a stored URL; empty for one that cannot be read, since SQLite would fail the
    whole query on an exception."""
    try:
        return domain(url)
    except ValueError:
        return ""


class Row(NamedTuple):
    """A cached page without its text or links."""

    key: str
    final_url: str
    title: str
    engine: Engine
    fetched_at: float
    pieces: int
    length: int


class Stats(NamedTuple):
    """What the cache holds and what it takes on disk."""

    pages: int
    links: int
    pieces: int
    text_bytes: int
    vector_bytes: int
    file_bytes: int
    path: str
    oldest: float | None
    newest: float | None


class PageStore:
    """Three tables and a text index in one file. Each operation opens its own connection, so any
    thread can call."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            # Readers do not block the writer, and the mode persists in the file.
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS pages ("
                "url TEXT PRIMARY KEY, final_url TEXT NOT NULL, fetched_at REAL NOT NULL, "
                "etag TEXT, last_modified TEXT, content_hash TEXT NOT NULL, "
                "engine TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS links ("
                "page TEXT NOT NULL, position INTEGER NOT NULL, target TEXT NOT NULL, "
                "anchor TEXT NOT NULL, context TEXT NOT NULL, region TEXT NOT NULL, "
                "PRIMARY KEY (page, position))"
            )
            # A vector is float32 bytes, and only meaningful for the model that made it.
            db.execute(
                "CREATE TABLE IF NOT EXISTS pieces ("
                "page TEXT NOT NULL, position INTEGER NOT NULL, text TEXT NOT NULL, "
                "model TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (page, position))"
            )
            # External content: the index holds no copy of the text; the triggers keep it in step.
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS pieces_fts USING fts5("
                "text, content='pieces', content_rowid='rowid')"
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS pieces_added AFTER INSERT ON pieces BEGIN "
                "INSERT INTO pieces_fts(rowid, text) VALUES (new.rowid, new.text); END"
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS pieces_dropped AFTER DELETE ON pieces BEGIN "
                "INSERT INTO pieces_fts(pieces_fts, rowid, text) "
                "VALUES ('delete', old.rowid, old.text); END"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            # Commits on success, rolls back on an exception.
            with db:
                yield db
        finally:
            db.close()

    def get(self, url: str) -> tuple[Page, str | None, str | None] | None:
        """The page cached for `url`, with its ETag and Last-Modified; its `url` is as given."""
        key = normalize(url)
        with self._connect() as db:
            row = db.execute(
                "SELECT final_url, fetched_at, etag, last_modified, engine, title, text "
                "FROM pages WHERE url = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            links = db.execute(
                "SELECT target, anchor, context, region FROM links WHERE page = ? "
                "ORDER BY position",
                (key,),
            ).fetchall()
        final_url, fetched_at, etag, last_modified, engine, title, text = row
        page = Page(
            url,
            final_url,
            title,
            text,
            cast(Engine, engine),
            [Link(*link) for link in links],
            fetched_at,
        )
        return page, etag, last_modified

    def rows(self) -> list[Row]:
        """Every page, newest first, without its text. Scans the table, which is small."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT url, final_url, title, engine, fetched_at, "
                "(SELECT COUNT(*) FROM pieces WHERE pieces.page = pages.url), length(text) "
                "FROM pages ORDER BY fetched_at DESC"
            ).fetchall()
        return [Row(*row) for row in rows]

    def stats(self) -> Stats:
        """Row counts, bytes of text and vectors, and the file with its WAL and shared-memory
        files."""
        with self._connect() as db:
            pages, text_bytes, oldest, newest = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(CAST(text AS BLOB))), 0), "
                "MIN(fetched_at), MAX(fetched_at) FROM pages"
            ).fetchone()
            links = db.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            pieces, vector_bytes = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(vector)), 0) FROM pieces"
            ).fetchone()
        file_bytes = sum(
            file.stat().st_size
            for file in (self._path, *(Path(f"{self._path}{tail}") for tail in ("-wal", "-shm")))
            if file.exists()
        )
        return Stats(
            pages,
            links,
            pieces,
            text_bytes,
            vector_bytes,
            file_bytes,
            str(self._path),
            oldest,
            newest,
        )

    def link_pairs(self) -> list[tuple[str, str]]:
        """The page and target URL of every stored link."""
        with self._connect() as db:
            return db.execute("SELECT page, target FROM links").fetchall()

    def flush(self) -> None:
        """Drops every page, link and piece, then returns the space to the disk."""
        with self._connect() as db:
            for table in ("pages", "links", "pieces"):
                db.execute(f"DELETE FROM {table}")
        # VACUUM refuses to run in a transaction, so it gets a connection that opens none. It
        # writes through the WAL, so the checkpoint that empties the WAL comes after.
        with closing(sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)) as db:
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def delete(self, keys: list[str]) -> None:
        """Drops the pages cached under the normalized URLs `keys`, with their links and pieces."""
        with self._connect() as db:
            for table, column in (("pages", "url"), ("links", "page"), ("pieces", "page")):
                db.executemany(f"DELETE FROM {table} WHERE {column} = ?", [(key,) for key in keys])

    def put(self, page: Page, etag: str | None, last_modified: str | None) -> None:
        """Replaces the page cached for its URL, and its links, in one transaction. Its pieces
        belonged to the old text, so they go."""
        key = normalize(page.url)
        digest = hashlib.sha256(page.text.encode()).hexdigest()
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    page.final_url,
                    page.fetched_at,
                    etag,
                    last_modified,
                    digest,
                    page.engine,
                    page.title,
                    page.text,
                ),
            )
            db.execute("DELETE FROM links WHERE page = ?", (key,))
            db.execute("DELETE FROM pieces WHERE page = ?", (key,))
            db.executemany(
                "INSERT INTO links VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (key, position, link.url, link.anchor, link.context, link.region)
                    for position, link in enumerate(page.links)
                ],
            )

    def touch(self, url: str) -> float:
        """Marks the page as read now, after its site said it had not changed. Returns that time."""
        now = time.time()
        with self._connect() as db:
            db.execute("UPDATE pages SET fetched_at = ? WHERE url = ?", (now, normalize(url)))
        return now

    def put_pieces(self, url: str, texts: list[str], vectors: np.ndarray, model: str) -> None:
        """Replaces the pieces kept for `url` with `texts` and their `vectors` from `model`."""
        key = normalize(url)
        rows = [
            (key, position, text, model, np.asarray(vector, dtype=np.float32).tobytes())
            for position, (text, vector) in enumerate(zip(texts, vectors, strict=True))
        ]
        with self._connect() as db:
            db.execute("DELETE FROM pieces WHERE page = ?", (key,))
            db.executemany("INSERT INTO pieces VALUES (?, ?, ?, ?, ?)", rows)

    def search(
        self,
        words: str,
        model: str,
        exclude_urls: list[str],
        limit: int = 200,
        exclude_hosts: Collection[str] = (),
    ) -> list[tuple[Page, str, np.ndarray]]:
        """The pieces of `model` that best match the words of `words` (BM25), at most `limit`,
        leaving out the pages in `exclude_urls` and those of the sites in `exclude_hosts`. Each
        comes with its page, without text or links."""
        terms = " OR ".join(f'"{word}"' for word in re.findall(r"\w+", words))
        if not terms:
            return []
        excluded = [normalize(url) for url in exclude_urls]
        host_clause = (
            f"AND host(pages.url) NOT IN ({','.join('?' * len(exclude_hosts))}) "
            if exclude_hosts
            else ""
        )
        with self._connect() as db:
            if exclude_hosts:
                db.create_function("host", 1, _host, deterministic=True)
            rows = db.execute(
                "SELECT pages.url, pages.final_url, pages.title, pages.engine, pages.fetched_at, "
                "pieces.text, pieces.vector FROM pieces_fts "
                "JOIN pieces ON pieces.rowid = pieces_fts.rowid "
                "JOIN pages ON pages.url = pieces.page "
                "WHERE pieces_fts MATCH ? AND pieces.model = ? "
                f"AND pieces.page NOT IN ({','.join('?' * len(excluded))}) "
                f"{host_clause}"
                "ORDER BY bm25(pieces_fts) LIMIT ?",
                (terms, model, *excluded, *exclude_hosts, limit),
            ).fetchall()
        return [
            (
                Page(url, final_url, title, "", cast(Engine, engine), [], fetched_at),
                text,
                np.frombuffer(vector, dtype=np.float32),
            )
            for url, final_url, title, engine, fetched_at, text, vector in rows
        ]
