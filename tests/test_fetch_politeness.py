"""Page requests are paced per host and say who is asking."""

from __future__ import annotations

import contextlib
import threading
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from kvasir import __version__
from kvasir.research import fetch
from kvasir.research.fetch import FetchError
from kvasir.storm import runtime


class _Clock:
    """Time that moves only when something sleeps, so a test never waits for real."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = 100.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        with self.lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.now += seconds
            self.slept.append(seconds)


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(runtime, "_sleep", clock.sleep)
    runtime.configure_fetch("", 1.0)
    # No host remembered from another test, whose times came from another clock.
    monkeypatch.setattr(runtime, "_hosts", {})
    yield clock
    runtime.configure_fetch("", runtime.DEFAULT_HOST_INTERVAL_SECONDS)


def test_requests_to_one_host_start_an_interval_apart(clock):
    starts = []
    for _ in range(3):
        with runtime.host_slot("a.example"):
            starts.append(clock.monotonic())

    assert starts == [100.0, 101.0, 102.0]


def test_another_host_does_not_wait_for_the_first(clock):
    with runtime.host_slot("a.example"):
        pass
    with runtime.host_slot("b.example"):
        pass

    assert clock.slept == []


def test_a_host_never_has_more_than_two_requests_open(clock):
    runtime.configure_fetch("", 0.0)
    lock = threading.Lock()
    now = peak = 0

    def request():
        nonlocal now, peak
        with runtime.host_slot("a.example"):
            with lock:
                now += 1
                peak = max(peak, now)
            threading.Event().wait(0.02)
            with lock:
                now -= 1

    threads = [threading.Thread(target=request) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == runtime.HOST_CONCURRENCY


def test_the_host_wait_holds_no_process_permit(clock, monkeypatch):
    free = []

    def sleep(seconds):
        # The only permit there is must be free while this host is being waited for.
        free.append(runtime._fetch_slots.acquire(blocking=False))
        runtime._fetch_slots.release()
        clock.sleep(seconds)

    monkeypatch.setattr(runtime, "_sleep", sleep)
    runtime.configure_concurrency(1)
    try:
        with runtime.host_slot("a.example"):
            pass
        with runtime.host_slot("a.example"):
            pass
    finally:
        runtime.configure_concurrency(runtime.DEFAULT_MAX_THREADS)

    assert free == [True]


def test_delay_host_pushes_the_next_start_later(clock):
    with runtime.host_slot("a.example"):
        pass
    runtime.delay_host("a.example", 7)
    with runtime.host_slot("a.example"):
        pass

    assert clock.monotonic() == 107.0


def test_delay_host_never_pulls_the_next_start_earlier(clock):
    with runtime.host_slot("a.example"):
        pass
    runtime.delay_host("a.example", 0)
    with runtime.host_slot("a.example"):
        pass

    assert clock.monotonic() == 101.0


def test_retry_after_in_seconds():
    assert fetch._retry_after("12") == 12.0


def test_retry_after_as_an_http_date():
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=10), usegmt=True)

    assert 8 < fetch._retry_after(date) <= 10


def test_a_retry_after_date_in_the_past_asks_for_no_wait():
    assert fetch._retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


@pytest.mark.parametrize("value", [None, "soon", ""])
def test_a_missing_or_unreadable_retry_after_waits_five_seconds(value):
    assert fetch._retry_after(value) == 5.0


def _serve(monkeypatch, answers):
    """Answer each request with the next of `answers`, a (status, headers). Returns the headers."""
    seen = []

    def stream(method, url, **kwargs):
        status, headers = answers[len(seen)]
        seen.append(kwargs["headers"])
        return contextlib.nullcontext(
            httpx.Response(
                status,
                headers=headers | {"content-type": "text/html"},
                content=b"<body><p>" + b"Kvasir " * 8 + b"</p></body>",
                request=httpx.Request(method, url),
            )
        )

    monkeypatch.setattr(fetch.httpx, "stream", stream)
    return seen


def test_a_429_is_waited_out_and_retried_once(clock, monkeypatch):
    seen = _serve(monkeypatch, [(429, {"retry-after": "3"}), (200, {})])

    assert fetch.fetch_page("https://a.example/x").text == ("Kvasir " * 8).strip()
    assert len(seen) == 2
    # The first request started at 100 and set the interval; the retry waited out the 3 seconds.
    assert clock.monotonic() == 103.0


def test_a_short_retry_after_delays_the_host_and_is_retried(clock, monkeypatch):
    delays = []
    delay_host = runtime.delay_host
    monkeypatch.setattr(
        runtime,
        "delay_host",
        lambda host, seconds: (delays.append((host, seconds)), delay_host(host, seconds)),
    )
    seen = _serve(monkeypatch, [(429, {"retry-after": "5"}), (200, {})])

    fetch.fetch_page("https://deepwiki.example/x")

    assert delays == [("deepwiki.example", 5.0)]
    assert len(seen) == 2


def test_a_429_without_retry_after_waits_five_seconds(clock, monkeypatch):
    _serve(monkeypatch, [(429, {}), (200, {})])

    fetch.fetch_page("https://a.example/x")

    assert clock.monotonic() == 105.0


def test_a_second_429_is_an_error(clock, monkeypatch):
    seen = _serve(monkeypatch, [(429, {"retry-after": "1"}), (429, {"retry-after": "1"})])

    with pytest.raises(FetchError, match="rate limited"):
        fetch.fetch_page("https://a.example/x")
    assert len(seen) == 2


def test_a_long_retry_after_is_not_waited_for(clock, monkeypatch):
    seen = _serve(monkeypatch, [(429, {"retry-after": "31"})])

    with pytest.raises(FetchError, match="rate limited, retry after 31s"):
        fetch.fetch_page("https://a.example/x")
    assert len(seen) == 1
    assert clock.slept == []


def test_the_user_agent_names_the_contact(clock, monkeypatch):
    runtime.configure_fetch("ops@example.org", 1.0)
    seen = _serve(monkeypatch, [(200, {})])

    fetch.fetch_page("https://a.example/x")

    assert seen[0]["User-Agent"] == (
        f"kvasir/{__version__} (+https://github.com/brewcoua/kvasir; ops@example.org)"
        f" httpx/{httpx.__version__}"
    )


def test_the_user_agent_without_a_contact_has_no_separator(clock, monkeypatch):
    seen = _serve(monkeypatch, [(200, {})])

    fetch.fetch_page("https://a.example/x")

    assert seen[0]["User-Agent"] == (
        f"kvasir/{__version__} (+https://github.com/brewcoua/kvasir) httpx/{httpx.__version__}"
    )
