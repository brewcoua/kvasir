"""The single place this package configures litellm and its own concurrency.

Upstream did this twice, in `lm.py` and `encoder.py`, as a side effect of importing either module.
Two things went wrong with that. The disk cache was opened under `Path.home()`, so importing the
package wrote to the filesystem and failed outright under a read-only root; and there was no way to
choose a different directory, or none at all, without editing the source.

Importing this module still sets the process-wide litellm flags below, because they are policy for
this fork rather than deployment configuration, and none of them touch the filesystem or the
network. The cache is the part that does, so it is opened only by an explicit `configure_cache`
call.

Concurrency lives here for the same reason. The pipeline nests thread pools, and upstream sized each
level independently, so the worst case multiplied out to hundreds of simultaneous requests against a
self-hosted gateway and search instance. One setting sizes every pool, and outbound search requests
take a permit from one process-wide budget. Those pools also carry the caller's context, so a run
stays identifiable in the logs after the pipeline fans out.
"""

import concurrent.futures
import contextvars
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol, TypeVar

import litellm
from litellm.caching.caching import Cache

DEFAULT_MAX_THREADS = 10

# Gateways route to models that reject parameters other models accept, and a rejected parameter
# should not fail a run.
litellm.drop_params = True
litellm.telemetry = False


def configure_cache(cache_dir: str | os.PathLike[str] | None) -> None:
    """Open a litellm disk cache under `cache_dir`, or disable caching when it is None.

    Idempotent. Not called on import, so a caller that never calls it runs uncached rather than
    writing somewhere it did not choose.
    """
    if cache_dir is None:
        litellm.cache = None
        return
    os.makedirs(cache_dir, exist_ok=True)
    litellm.cache = Cache(disk_cache_dir=str(cache_dir), type="disk")


_max_threads = DEFAULT_MAX_THREADS
_fetch_slots = threading.BoundedSemaphore(DEFAULT_MAX_THREADS)


def configure_concurrency(max_threads: int) -> None:
    """Set how wide the pipeline's thread pools run, and how many fetches may be in flight.

    Idempotent, but only between runs: replacing the semaphore while one is held would lose the
    outstanding permits.
    """
    global _max_threads, _fetch_slots
    _max_threads = max_threads
    _fetch_slots = threading.BoundedSemaphore(max_threads)


def max_threads() -> int:
    """How wide one thread pool in the pipeline may run."""
    return _max_threads


@contextmanager
def fetch_slot() -> Iterator[None]:
    """Hold one of the process's fetch permits for a single outbound request.

    Retrieval is the innermost level of the pipeline and waits on no future of its own, so a permit
    is never held across a wait and this cannot deadlock against the pools nested above it.
    """
    _fetch_slots.acquire()
    try:
        yield
    finally:
        _fetch_slots.release()


class UsageSink(Protocol):
    """Where the pipeline reports what a run spent. Implemented outside this package."""

    def record_lm(
        self, model: str, role: str | None, prompt_tokens: int, completion_tokens: int, cost: float
    ) -> None: ...

    def record_embedding(self, model: str, tokens: int) -> None: ...

    def record_search(self, engine: str, queries: int) -> None: ...


# A contextvar rather than a global, because a sink belongs to one run and the pipeline reports
# from every thread that run touches. The pools above carry it along with everything else.
_usage_sink: ContextVar[UsageSink | None] = ContextVar("usage_sink", default=None)


@contextmanager
def record_usage_into(sink: UsageSink) -> Iterator[None]:
    """Send everything this thread and its pools spend to `sink`."""
    token = _usage_sink.set(sink)
    try:
        yield
    finally:
        _usage_sink.reset(token)


def record_lm_usage(
    model: str, role: str | None, prompt_tokens: int, completion_tokens: int, cost: float
) -> None:
    """Report one completion. A no-op when nothing is listening, which is the library case."""
    sink = _usage_sink.get()
    if sink is not None:
        sink.record_lm(model, role, prompt_tokens, completion_tokens, cost)


def record_embedding_usage(model: str, tokens: int) -> None:
    sink = _usage_sink.get()
    if sink is not None:
        sink.record_embedding(model, tokens)


def record_search_usage(engine: str, queries: int) -> None:
    sink = _usage_sink.get()
    if sink is not None:
        sink.record_search(engine, queries)


_T = TypeVar("_T")


class ContextThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """A pool whose tasks run in a copy of the submitting thread's context.

    A worker thread starts with an empty context, so a run's identity would be lost the moment the
    pipeline fans out — which is most of a run, and the part worth having logs for. Copying at
    submit time rather than at construction is what makes the copy reflect the stage the run was
    actually in when the work was handed over.
    """

    def submit(  # type: ignore[override]
        self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> concurrent.futures.Future[_T]:
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


__all__ = [
    "Cache",
    "ContextThreadPoolExecutor",
    "DEFAULT_MAX_THREADS",
    "UsageSink",
    "configure_cache",
    "configure_concurrency",
    "fetch_slot",
    "litellm",
    "max_threads",
    "record_embedding_usage",
    "record_lm_usage",
    "record_search_usage",
    "record_usage_into",
]
