"""Logging configuration, and the run identity every record carries.

The service does one slow thing at a time and does it across many threads, so a log line is close
to useless without knowing which run and which stage produced it. That identity lives in
contextvars rather than being threaded through call arguments, because most of the lines come from
inside the vendored pipeline, which has no idea kvasir exists.

`kvasir.storm.runtime.ContextThreadPoolExecutor` is what carries the identity across the pipeline's
thread pools. Owning the fork is what makes that possible.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from kvasir.config import Settings

_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)
_run_kind: ContextVar[str | None] = ContextVar("run_kind", default=None)
_stage: ContextVar[str | None] = ContextVar("stage", default=None)

# Everything a LogRecord carries by default. Anything else on a record was put there by a caller,
# so it is worth emitting rather than guessing at a fixed set of extras.
_STANDARD = frozenset(
    set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)
    | {"message", "asctime", "taskName", "run_id", "run_kind", "stage"}
)


@contextmanager
def run_context(run_id: str, run_kind: str) -> Iterator[None]:
    """Tag every record emitted from this thread, and from pools it spawns, with a run."""
    tokens = (_run_id.set(run_id), _run_kind.set(run_kind), _stage.set(None))
    try:
        yield
    finally:
        _run_id.reset(tokens[0])
        _run_kind.reset(tokens[1])
        _stage.reset(tokens[2])


def set_stage(stage: str) -> None:
    """Record which stage the current thread is in. Pools spawned after this inherit it."""
    _stage.set(stage)


class RunContextFilter(logging.Filter):
    """Copies the run identity onto every record. A filter, so it applies to third-party loggers.

    A value passed as `extra` wins, which is how a caller outside the run's own thread — the event
    loop reporting that the run failed, say — still attributes its line to that run.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, var in (("run_id", _run_id), ("run_kind", _run_kind), ("stage", _stage)):
            if not hasattr(record, key):
                setattr(record, key, var.get())
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for a log shipper to parse.

    Falls back to `str` for anything unserialisable rather than dropping the record: a log line is
    never important enough to fail the work that produced it.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("run_id", "run_kind", "stage"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        payload.update(
            {key: value for key, value in record.__dict__.items() if key not in _STANDARD}
        )
        return json.dumps(payload, default=str)


def configure(settings: Settings) -> None:
    """Install the root handler. Idempotent: repeated calls replace the handler rather than add one.

    This is the only place the root logger is configured. The vendored tree used to call
    `basicConfig` on import, which stole the configuration from whatever embedded it.
    """
    handler = logging.StreamHandler()
    handler.addFilter(RunContextFilter())
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s [%(run_id)s/%(stage)s] %(message)s"
            )
        )

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)
