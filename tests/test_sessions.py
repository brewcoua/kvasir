import json
import os
import time

import pytest

from kvasir.sessions import SessionIdError, SessionNotFound, SessionStore, validate_id

STATE = {"runner_argument": {"topic": "The Kvasir stone"}, "conversation_history": []}


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "sessions", ttl_hours=168)


@pytest.mark.parametrize(
    "session_id",
    [
        "../escape",
        "sub/dir",
        "sub\\dir",
        "..",
        ".",
        ".hidden",
        "-leading-hyphen",
        "with space",
        "with\x00null",
        "",
        "a" * 129,
        "/absolute",
        "trailing/",
    ],
)
def test_unsafe_ids_are_rejected(session_id):
    with pytest.raises(SessionIdError):
        validate_id(session_id)


@pytest.mark.parametrize("session_id", ["a", "chat-123", "Chat_123", "0", "a" * 128])
def test_safe_ids_are_accepted(session_id):
    assert validate_id(session_id) == session_id


def test_traversal_cannot_escape_the_directory(store, tmp_path):
    with pytest.raises(SessionIdError):
        store.save("../../etc/passwd", STATE)

    assert not (tmp_path.parent / "passwd").exists()


def test_round_trip(store):
    store.save("chat-1", STATE)

    assert store.exists("chat-1")
    assert store.load("chat-1") == STATE


def test_missing_session_raises(store):
    for operation in (store.load, store.delete, store.updated_at):
        with pytest.raises(SessionNotFound):
            operation("absent")


def test_delete_removes_the_file(store):
    store.save("chat-1", STATE)
    store.delete("chat-1")

    assert not store.exists("chat-1")


def test_a_crash_mid_write_leaves_the_previous_session_intact(store, monkeypatch):
    store.save("chat-1", STATE)

    def crash(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        store.save("chat-1", {"runner_argument": {"topic": "replacement"}})

    # The rename never happened, so the readable session is still the old one.
    assert store.load("chat-1") == STATE


def test_a_failed_write_leaves_no_temporary_file(store, monkeypatch, tmp_path):
    monkeypatch.setattr(json, "dump", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("x")))

    with pytest.raises(OSError):
        store.save("chat-1", STATE)

    assert list((tmp_path / "sessions").glob("*")) == []


def test_sweep_removes_only_expired_sessions(tmp_path):
    store = SessionStore(tmp_path / "sessions", ttl_hours=1)
    store.save("fresh", STATE)
    store.save("stale", STATE)
    old = time.time() - 2 * 3600
    os.utime(store.path("stale"), (old, old))

    assert store.sweep() == 1
    assert store.exists("fresh")
    assert not store.exists("stale")


def test_sweep_clears_temporary_files_left_by_a_crash(store, tmp_path):
    store.save("chat-1", STATE)
    (tmp_path / "sessions" / "chat-1.json.tmp").write_text("half written")

    store.sweep()

    assert list((tmp_path / "sessions").glob("*.tmp")) == []
    assert store.exists("chat-1")


def test_the_directory_is_created_on_demand(tmp_path):
    SessionStore(tmp_path / "deep" / "sessions", ttl_hours=1)

    assert (tmp_path / "deep" / "sessions").is_dir()
