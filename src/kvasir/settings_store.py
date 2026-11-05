"""The settings changed from their defaults, kept in `kvasir.db` so the API can edit them.

One row per field, keyed by its path such as `research.max_searches`, holding its value as JSON. A
field with no row has its default, so a new field needs no migration. The values are decoded and
checked by `kvasir.config`, not here, so one bad row costs only its own field.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

BUSY_TIMEOUT_SECONDS = 30.0


class SettingsStore:
    """One table. Each operation opens its own connection, so any thread may call it."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
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

    def load(self) -> dict[str, str]:
        """Each stored field's JSON text, by key."""
        with self._connect() as db:
            return dict(db.execute("SELECT * FROM settings"))

    def write(self, changes: Mapping[str, Any]) -> None:
        """Sets each field in one transaction. None removes a field's row, giving it its default."""
        with self._connect() as db:
            for key, value in changes.items():
                if value is None:
                    db.execute("DELETE FROM settings WHERE key = ?", (key,))
                else:
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value))
                    )
