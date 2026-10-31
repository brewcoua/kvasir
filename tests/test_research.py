"""The deep research pipeline, with every model call, search and page faked."""

from __future__ import annotations

import pytest
from dspy.utils.dummies import DummyLM

from kvasir.config import Settings
from kvasir.deep import fetch, pipeline
from kvasir.deep.fetch import FetchError, extract, split
from kvasir.deep.render import render
from kvasir.deep.store import Claim
from kvasir.models import Clarification, ResearchRequest

PAGE = (
    "<html><body><nav>Home | About</nav><h1>The stone</h1>"
    "<p>It was found in 1904 near Kvasir.</p><script>track()</script></body></html>"
)


class Stream:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def publish(self, stage, detail, done=None, total=None):
        self.events.append((stage, detail))


def claim(id, url, quote="q"):
    return Claim(
        id=id, text=f"claim {id}", quote=quote, url=url, title=url, confidence="high", round=1
    )


def test_extract_keeps_the_text_and_drops_page_furniture():
    assert extract(PAGE) == "The stone\nIt was found in 1904 near Kvasir."


def test_split_cuts_between_lines_and_hard_cuts_a_long_line():
    assert split("aaa\nbbb\ncc", size=7) == ["aaa\nbbb", "cc"]
    assert split("abcdefghij", size=4) == ["abcd", "efgh", "ij"]


def test_a_page_that_is_not_html_is_refused(monkeypatch):
    class Response:
        headers = {"content-type": "application/pdf"}
        text = ""

        def raise_for_status(self):
            pass

    monkeypatch.setattr(fetch.httpx, "get", lambda url, **kwargs: Response())
    with pytest.raises(FetchError, match="not HTML"):
        fetch.fetch_text("https://example.org/a.pdf")


def test_render_numbers_sources_by_page_and_drops_made_up_ids():
    claims = {1: claim(1, "https://a"), 2: claim(2, "https://b"), 3: claim(3, "https://a")}

    report, citations = render(
        [("Findings", "First [2]. Second [1][3]. Third [3, 2]. Invented [9].")], claims
    )

    assert report == "## Findings\n\nFirst [1]. Second [2]. Third [2][1]. Invented ."
    assert [(c.index, c.url) for c in citations] == [(1, "https://b"), (2, "https://a")]


@pytest.fixture
def settings():
    return Settings.from_env(
        {
            "OPENAI_API_KEY": "key",
            "OPENAI_API_BASE": "https://gateway.example/v1",
            "KVASIR_MODEL_FAST": "openai/fast",
            "KVASIR_MODEL_STRONG": "openai/strong",
            "KVASIR_SEARXNG_URL": "http://searxng.example",
        }
    )


def fake_models(monkeypatch, answers):
    models = {role: DummyLM(replies) for role, replies in answers.items()}
    monkeypatch.setattr(pipeline, "build_research_models", lambda *args: models)


def test_a_run_reads_pages_keeps_quoted_claims_and_cites_them(settings, monkeypatch):
    fake_models(
        monkeypatch,
        {
            "brief": [{"brief": "When was the stone found?", "assumptions": ["any source"]}],
            "plan": [{"queries": ["kvasir stone found"]}],
            "intake": [
                {
                    "findings": [
                        {
                            "claim": "Found in 1904",
                            "quote": "found in   1904",
                            "confidence": "high",
                        },
                        {"claim": "Made of gold", "quote": "solid gold", "confidence": "low"},
                    ]
                },
                {"findings": []},
            ],
            "notes": [{"notes_out": "Found in 1904 [1]."}],
            "outline": [{"sections": ["Findings"]}],
            "write": [{"text": "It was found in 1904 [1][7]."}],
        },
    )
    hits = [
        {"url": "https://a", "title": "A", "description": "snippet a", "snippets": []},
        {"url": "https://b", "title": "B", "description": "snippet b", "snippets": []},
    ]
    monkeypatch.setattr(
        pipeline, "build_research_tools", lambda settings: (None, lambda q, exclude_urls: hits)
    )

    def fake_fetch(url):
        if url == "https://b":
            raise FetchError("blocked")
        return extract(PAGE)

    monkeypatch.setattr(pipeline, "fetch_text", fake_fetch)
    stream = Stream()

    result = pipeline.run_research(
        settings, ResearchRequest(question="When was the Kvasir stone found?"), stream
    )

    # The paraphrased "solid gold" quote is on no page, so it is not evidence. One new claim is
    # below the saturation bar, so the run stops after one round rather than asking for a plan.
    assert [(c.id, c.text, c.url) for c in result.claims] == [(1, "Found in 1904", "https://a")]
    assert result.report == "## Findings\n\nIt was found in 1904 [1]."
    assert [(c.index, c.url, c.snippet) for c in result.citations] == [
        (1, "https://a", "found in   1904")
    ]
    assert result.assumptions == ["any source"]
    assert [stage for stage, _ in stream.events][:2] == ["brief", "research"]
    # One event per piece read, each naming its page, the snippet fallback included.
    assert ("research", "round 1: reading A") in stream.events
    assert ("research", "round 1: reading B") in stream.events
    assert stream.events[-1] == ("write", "assembled the report")


def test_unreadable_claims_cost_the_piece_not_the_run(monkeypatch):
    model = DummyLM([{"nonsense": "x"}] * 4)
    hit = {"url": "https://a", "title": "A"}

    assert pipeline._intake(model, "brief", hit, "text", 1, 1) == []


def test_clarifications_reach_the_brief_as_text():
    text = pipeline._clarifications([Clarification(question="Region?", answer="Europe")])

    assert text == "Q: Region?\nA: Europe"
    assert pipeline._clarifications([]) == "none"


def test_clarify_asks_at_most_three_questions(settings, monkeypatch):
    fake_models(monkeypatch, {"clarify": [{"questions": ["a?", "b?", "c?", "d?"]}]})

    assert pipeline.clarify(settings, "best battery makers") == ["a?", "b?", "c?"]
