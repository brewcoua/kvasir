"""Every line is parseable, and carries the run that produced it."""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest
import uvicorn

from kvasir import __main__, logs
from kvasir.config import Settings
from kvasir.storm.runtime import ContextThreadPoolExecutor

MINIMAL = {
    "OPENAI_API_KEY": "key",
    "OPENAI_API_BASE": "https://gateway.example/v1",
    "KVASIR_MODEL_FAST": "openai/ollama/fast:cloud",
    "KVASIR_MODEL_STRONG": "openai/ollama/strong:cloud",
    "KVASIR_SEARXNG_URL": "http://searxng.example",
}


@pytest.fixture
def emit(capsys):
    """Configure the root logger, and give back a way to read what it wrote."""
    before = logging.getLogger().handlers[:]

    def configure(log_format="json"):
        logs.configure(replace(Settings.from_env(MINIMAL), log_format=log_format))

        def lines():
            return [line for line in capsys.readouterr().err.splitlines() if line]

        return lines

    yield configure

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in before:
        root.addHandler(handler)


def test_every_line_is_json(emit):
    lines = emit()

    logging.getLogger("kvasir.test").info("plain")
    logging.getLogger("kvasir.test").warning("with a %s", "substitution")

    records = [json.loads(line) for line in lines()]
    assert [record["message"] for record in records] == ["plain", "with a substitution"]
    assert [record["level"] for record in records] == ["INFO", "WARNING"]
    # Absent rather than null, so a run-less line does not look like a run with no id.
    assert "run_id" not in records[0]


def test_a_line_carries_its_run(emit):
    lines = emit()

    with logs.run_context("abc123", "storm"):
        logs.set_stage("research")
        logging.getLogger("kvasir.test").info("searching")

    record = json.loads(lines()[0])
    assert record["run_id"] == "abc123"
    assert record["run_kind"] == "storm"
    assert record["stage"] == "research"


def test_the_run_survives_a_fan_out(emit):
    """The pipeline spends most of a run inside thread pools, which start with an empty context."""
    lines = emit()

    def work():
        logging.getLogger("kvasir.test").info("in a worker")

    with logs.run_context("abc123", "storm"):
        logs.set_stage("article")
        with ContextThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: work(), range(4)))

    records = [json.loads(line) for line in lines()]
    assert len(records) == 4
    assert {record["run_id"] for record in records} == {"abc123"}
    assert {record["stage"] for record in records} == {"article"}


def test_a_stage_set_inside_a_worker_stays_there(emit):
    """Each task gets its own copy, so one worker cannot rename the stage for the whole run."""
    lines = emit()

    with logs.run_context("abc123", "storm"):
        logs.set_stage("article")
        with ContextThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(logs.set_stage, "somewhere else").result()
        logging.getLogger("kvasir.test").info("back on the run thread")

    assert json.loads(lines()[0])["stage"] == "article"


def test_an_explicit_run_wins_over_the_context(emit):
    """How the event loop attributes a failure to a run it is not running inside."""
    lines = emit()

    logging.getLogger("kvasir.test").error("failed", extra={"run_id": "abc123"})

    assert json.loads(lines()[0])["run_id"] == "abc123"


def test_an_exception_is_recorded(emit):
    lines = emit()

    try:
        raise ValueError("no sources")
    except ValueError:
        logging.getLogger("kvasir.test").exception("stage failed")

    record = json.loads(lines()[0])
    assert "ValueError: no sources" in record["exception"]


def test_text_format_is_readable(emit):
    lines = emit("text")

    with logs.run_context("abc123", "storm"):
        logs.set_stage("research")
        logging.getLogger("kvasir.test").info("searching")

    assert "[abc123/research] searching" in lines()[0]


def test_configuring_twice_does_not_duplicate_lines(emit):
    emit()
    lines = emit()

    logging.getLogger("kvasir.test").info("once")

    assert len(lines()) == 1


def test_the_entrypoint_configures_logging_before_uvicorn_can(emit, monkeypatch, capsys):
    """Started as `uvicorn kvasir.main:app`, uvicorn pins its own handler on `uvicorn.access` and
    stops it propagating, so the access log printed as plain text next to everyone else's JSON.

    `emit` is taken only for its teardown, which puts the root handlers back.
    """
    for name, value in MINIMAL.items():
        monkeypatch.setenv(name, value)
    seen = {}

    def fake_run(target, **kwargs):
        # Uvicorn's loggers reach the root handler only because it installs none of its own.
        seen["log_config"] = kwargs["log_config"]
        logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /v1/runs HTTP/1.1" 200')

    monkeypatch.setattr(uvicorn, "run", fake_run)

    __main__.main()

    assert seen["log_config"] is None
    assert json.loads(capsys.readouterr().err.splitlines()[0])["logger"] == "uvicorn.access"
