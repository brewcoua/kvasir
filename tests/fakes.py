"""Fakes for the tests: SearXNG, the progress stream, the encoder, claims and methods."""

from __future__ import annotations

from dataclasses import replace

from kvasir.config import Settings
from kvasir.methods import METHODS
from kvasir.research.claims import Claim

PAGE = (
    "<html><body><nav>Home | About</nav><h1>The stone</h1>"
    "<p>It was found in 1904 near Kvasir.</p><script>track()</script></body></html>"
)


class Search:
    """SearXNG, answering every query with the same results, or failing as told."""

    def __init__(self, hits, failures=()):
        self.hits = hits
        self.failures = []
        self._failures = list(failures)

    def __call__(self, query, exclude_urls):
        self.failures = self._failures
        return [] if self._failures else self.hits


class Stream:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def publish(self, stage, detail, done=None, total=None):
        self.events.append((stage, detail))


class Encoder:
    """Every text the same vector, so nothing is closer than anything else and none repeats."""

    def encode(self, texts):
        return [[0.0]] * len(texts)


def claim(id, url, quote="q"):
    return Claim(
        id=id, text=f"claim {id}", quote=quote, url=url, title=url, confidence="high", step=1
    )


def research_settings() -> Settings:
    return Settings.from_env(
        {
            "OPENAI_API_KEY": "key",
            "OPENAI_API_BASE": "https://gateway.example/v1",
            "KVASIR_MODEL_FAST": "openai/fast",
            "KVASIR_MODEL_STRONG": "openai/strong",
            "KVASIR_SEARXNG_URL": "http://searxng.example",
        }
    )


def stub_method(monkeypatch, name, run):
    """Swaps the run of method `name` for `run`, where `POST /v1/runs` looks it up."""
    monkeypatch.setitem(METHODS, name, replace(METHODS[name], run=run))


def article_run(topic, **options):
    """A `POST /v1/runs` body for an article on `topic`."""
    return {"method": "storm", "input": {"question": topic}, "options": options}
