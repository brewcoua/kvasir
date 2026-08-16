"""The single place this package configures litellm and its own concurrency.

Upstream did this twice, in `lm.py` and `encoder.py`, as a side effect of importing either module.
Two things went wrong with that. The disk cache was opened under `Path.home()`, so importing the
package wrote to the filesystem and failed outright under a read-only root; and there was no way to
choose a different directory, or none at all, without editing the source.

Importing this module still sets the process-wide litellm flags below, because they are policy for
this fork rather than deployment configuration, and none of them touch the filesystem or the
network. The cache is the part that does, so it is opened only by an explicit `configure_cache`
call.

Concurrency lives here for the same reason. The pipeline nests thread pools three deep — section
writing fans out to retrieval, which fans out to page fetches — and upstream sized each level
independently, so the worst case multiplied out to hundreds of simultaneous requests against a
self-hosted gateway and search instance. One setting sizes every pool, and outbound requests take a
permit from one process-wide budget. Those pools also carry the caller's context, so a run stays
identifiable in the logs after the pipeline fans out.
"""

import concurrent.futures
import contextvars
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

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


def acquire_fetch_slot() -> None:
    """Claim one of the process's fetch permits, blocking until one is free.

    Callers that fan out over a thread pool acquire before submitting, so a permit bounds the
    threads created as well as the requests in flight. Release from the task itself.

    Only outbound search and page requests take a permit, and neither waits on a future while
    holding one, so this cannot deadlock against the pools nested above it.
    """
    _fetch_slots.acquire()


def release_fetch_slot() -> None:
    _fetch_slots.release()


@contextmanager
def fetch_slot() -> Iterator[None]:
    """Hold a fetch permit for the duration of a single outbound request."""
    acquire_fetch_slot()
    try:
        yield
    finally:
        release_fetch_slot()


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
    "acquire_fetch_slot",
    "configure_cache",
    "configure_concurrency",
    "fetch_slot",
    "litellm",
    "max_threads",
    "release_fetch_slot",
]
