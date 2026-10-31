"""Runs kept in SQLite, so they survive a restart. Used when KVASIR_RUN_STORE is sqlite.

A run is written twice: when it starts, so a restart mid-run leaves a trace of it, and when it ends,
with everything observed. Nothing is written in between, so a run in progress is read from memory.

The summary is stored beside the detail because a list reads every row, and a detail carries the
whole result.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from kvasir.models import RunDetail, RunSummary

if TYPE_CHECKING:
    from kvasir.runs import Run

logger = logging.getLogger("kvasir")

SECONDS_PER_DAY = 86400

INTERRUPTED = "interrupted by a restart"


class RunStore:
    """One table in one file. Each operation opens its own connection, so any thread may call it."""

    def __init__(self, path: Path, ttl_days: int, limit: int) -> None:
        self._path = path
        self._ttl_seconds = ttl_days * SECONDS_PER_DAY
        self._limit = limit
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "id TEXT PRIMARY KEY, created_at REAL NOT NULL, state TEXT NOT NULL, "
                "summary TEXT NOT NULL, detail TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path)
        try:
            # Commits on success, rolls back on an exception.
            with db:
                yield db
        finally:
            db.close()

    def save(self, run: Run) -> None:
        """Losing a record must not fail the run it records, so a write error is only logged."""
        summary = run.summary()
        detail = run.detail()
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?)",
                    (
                        run.id,
                        summary.created_at,
                        summary.state,
                        summary.model_dump_json(),
                        detail.model_dump_json(),
                    ),
                )
                self._prune(db)
        except sqlite3.Error:
            logger.exception("could not save run %s", run.id)

    def summaries(self) -> list[RunSummary]:
        """Newest first."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT summary FROM runs ORDER BY created_at DESC LIMIT ?", (self._limit,)
            ).fetchall()
        return [RunSummary.model_validate_json(row[0]) for row in rows]

    def detail(self, run_id: str) -> RunDetail | None:
        with self._connect() as db:
            row = db.execute("SELECT detail FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else RunDetail.model_validate_json(row[0])

    def sweep(self) -> None:
        """Drop what is past retention, and end the runs a restart cut short. Called at startup."""
        with self._connect() as db:
            self._prune(db)
            rows = db.execute(
                "SELECT id, summary, detail FROM runs WHERE state IN ('queued', 'running')"
            ).fetchall()
            for run_id, summary, detail in rows:
                db.execute(
                    "UPDATE runs SET state = 'failed', summary = ?, detail = ? WHERE id = ?",
                    (_interrupted(summary), _interrupted(detail), run_id),
                )
        if rows:
            logger.info("marked %d run(s) interrupted by the restart", len(rows))

    def _prune(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM runs WHERE created_at < ?", (time.time() - self._ttl_seconds,))
        db.execute(
            "DELETE FROM runs WHERE id NOT IN "
            "(SELECT id FROM runs ORDER BY created_at DESC LIMIT ?)",
            (self._limit,),
        )


def _interrupted(record: str) -> str:
    """A run saved as it started, ended as failed.

    Only the start was written, so there are no stages or calls to close, and the start is the
    last moment the run is known to have been alive.
    """
    data = json.loads(record)
    data["state"] = "failed"
    data["error"] = INTERRUPTED
    data["ended_at"] = data["started_at"] or data["created_at"]
    return json.dumps(data)
