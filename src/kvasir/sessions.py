"""Co-STORM session persistence.

A session is `CoStormRunner.to_dict()` written as JSON, one file per session. Ids come from the
caller so Open WebUI can key them by chat id, which is why they are validated as path components
before ever reaching the filesystem.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("kvasir")

# Deliberately narrow. Anything outside this cannot traverse, cannot hide a separator and cannot
# name a device, so no further path checking is needed.
_VALID_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")

SECONDS_PER_HOUR = 3600


class SessionIdError(ValueError):
    """The caller supplied an id that is not a safe path component."""


class SessionNotFound(LookupError):
    """No session with that id, or it expired."""


def validate_id(session_id: str) -> str:
    if not _VALID_ID.match(session_id):
        raise SessionIdError(
            "session id must be 1 to 128 characters of letters, digits, hyphen or underscore, "
            "starting with a letter or digit"
        )
    return session_id


class SessionStore:
    """Session files under one directory. No database, no index, no background task."""

    def __init__(self, directory: Path, ttl_hours: int) -> None:
        self._directory = directory
        self._ttl_seconds = ttl_hours * SECONDS_PER_HOUR
        self._directory.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: str) -> Path:
        return self._directory / f"{validate_id(session_id)}.json"

    def exists(self, session_id: str) -> bool:
        return self.path(session_id).is_file()

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        """Write atomically, so a crash mid-write leaves the previous session intact.

        The temporary file is a sibling because os.replace is only atomic within one filesystem.
        """
        destination = self.path(session_id)
        temporary = destination.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def load(self, session_id: str) -> dict[str, Any]:
        path = self.path(session_id)
        try:
            state: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SessionNotFound(session_id) from None
        return state

    def delete(self, session_id: str) -> None:
        try:
            self.path(session_id).unlink()
        except FileNotFoundError:
            raise SessionNotFound(session_id) from None

    def updated_at(self, session_id: str) -> float:
        try:
            return self.path(session_id).stat().st_mtime
        except FileNotFoundError:
            raise SessionNotFound(session_id) from None

    def sweep(self) -> int:
        """Delete sessions older than the TTL. Called once at startup, never on a timer."""
        cutoff = time.time() - self._ttl_seconds
        removed = 0
        for path in self._directory.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        # Left behind only by a crash between writing and renaming.
        for stale in self._directory.glob("*.json.tmp"):
            stale.unlink(missing_ok=True)
        if removed:
            logger.info("removed %d expired session(s)", removed)
        return removed
