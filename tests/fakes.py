"""Fakes for the tests: SearXNG, the progress stream, the encoder, claims and methods."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from dspy.utils.dummies import DummyLM

from kvasir.config import Settings
from kvasir.methods import METHODS
from kvasir.research.claims import Claim
from kvasir.research.fetch import Page
from kvasir.research.sources import Source

# The token every test environment sets, as a client sends it.
AUTH = {"Authorization": "Bearer token"}

PAGE = (
    "<html><body><nav>Home | About</nav><h1>The stone</h1>"
    "<p>It was found in 1904 near Kvasir.</p><script>track()</script></body></html>"
)


def page(text: str) -> Page:
    """A fetched page holding `text`, with no links."""
    return Page("https://a", "https://a", "", text, "static", [], 0.0)


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
        # Every event's step fields and counts, in step with `events`, whose index is its seq.
        self.fields: list[dict] = []

    def publish(self, stage, detail, done=None, total=None, **fields):
        self.events.append((stage, detail))
        self.fields.append({"done": done, "total": total, **fields})
        return len(self.events) - 1


class Encoder:
    """Every text the same vector, so nothing is closer than anything else and none repeats."""

    embedding_model_name = "fake"

    def encode(self, texts, query=False):
        return [[0.0]] * len(texts)


class Vectors:
    """An encoder giving each text the vector it was told, and any other text `default`."""

    embedding_model_name = "fake"

    def __init__(self, vectors, default=None):
        self.vectors = vectors
        self.default = default

    def encode(self, texts, query=False):
        return np.array([self.vectors.get(text, self.default) for text in texts], dtype=float)


class Logged(DummyLM):
    """A fake model that appends its name to `log` on every call, to show the order of calls."""

    def __init__(self, name, log, answers=()):
        super().__init__(list(answers))
        self.name, self.log = name, log

    def __call__(self, *args, **kwargs):
        self.log.append(self.name)
        return super().__call__(*args, **kwargs)


def claim(id, url, quote=None):
    quote = quote or f"quote #{id}."
    return Claim(
        id=id, text=f"claim {id}", quote=quote, url=url, title=url, confidence="high", step=1
    )


# A kind and tags that `kvasir.research.sources.tier` maps to each tier.
_BY_TIER = {
    "high": ("government", frozenset()),
    "normal": ("news", frozenset()),
    "unknown": ("unknown", frozenset()),
    "low": ("unknown", frozenset({"rsp_unreliable"})),
    "avoid": ("unknown", frozenset({"deny"})),
}


class Tiers:
    """Sources, classifying each host as the tier it was told and any other as `unknown`. Records
    the hosts of each call."""

    def __init__(self, **tiers):
        # Keyword names cannot hold dots, so `a_org` stands for `a.org`.
        self.tiers = {host.replace("_", "."): tier for host, tier in tiers.items()}
        self.calls = []
        self.deadlines = []

    def classify(self, hosts, deadline=5.0):
        hosts = list(hosts)
        self.calls.append(set(hosts))
        self.deadlines.append(deadline)
        found = {}
        for host in hosts:
            kind, tags = _BY_TIER[self.tiers.get(host, "unknown")]
            found[host] = Source(host=host, kind=kind, tags=tags)
        return found


def research_settings() -> Settings:
    return Settings.from_env(
        {
            "KVASIR_PROVIDER_GATEWAY_API_KEY": "key",
            "KVASIR_PROVIDER_GATEWAY_API_BASE": "https://gateway.example/v1",
            "KVASIR_MODEL_FAST": "gateway/fast",
            "KVASIR_MODEL_STRONG": "gateway/strong",
            "KVASIR_MODEL_EMBEDDING": "gateway/embed",
            "KVASIR_SEARCH_URL": "http://searxng.example",
            "KVASIR_TOKEN": "token",
        }
    )


def stub_method(monkeypatch, name, run):
    """Swaps the run of method `name` for `run`, where `POST /v1/runs` looks it up."""
    monkeypatch.setitem(METHODS, name, replace(METHODS[name], run=run))


def article_run(topic, **options):
    """A `POST /v1/runs` body for an article on `topic`."""
    return {"method": "storm", "input": {"question": topic}, "options": options}
