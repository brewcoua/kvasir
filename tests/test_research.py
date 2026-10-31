"""The deep research pipeline, with every model call, search and page faked."""

from __future__ import annotations

import pytest
from dspy.utils.dummies import DummyLM

from kvasir.config import Settings
from kvasir.deep import fetch, pipeline, verify
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


def test_render_drops_a_heading_that_repeats_the_section_title():
    report, _ = render([("Key Findings", "### Key Findings\n\nText.\n\n### Detail\n\nMore.")], {})

    assert report == "## Key Findings\n\nText.\n\n### Detail\n\nMore."


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
            "write": [{"text": "It was found in 1904 [1][7]. It is made of gold [1]."}],
            "verify": [
                {"supported": True, "reason": "the quote gives the year"},
                {"supported": False, "reason": "the quote says nothing about gold"},
            ],
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
    assert [(r.sentence, r.reason) for r in result.removed] == [
        ("It is made of gold [1].", "the quote says nothing about gold")
    ]
    assert [(c.index, c.url, c.snippet) for c in result.citations] == [
        (1, "https://a", "found in   1904")
    ]
    assert result.assumptions == ["any source"]
    assert [stage for stage, _ in stream.events][:2] == ["brief", "research"]
    # One event per piece read, each naming its page, the snippet fallback included.
    assert ("research", "round 1: page 1/2, part 1/1: A") in stream.events
    assert ("research", "round 1: page 2/2, part 1/1: B") in stream.events
    assert stream.events[-1] == ("verify", "removed 1 of 2 cited sentences")


@pytest.mark.parametrize(
    "quote",
    [
        # Each one a quote gpt-oss wrote that the page supports, from a live run.
        "trained using the Fill\u2011in\u2011the\u2011Middle objective",
        "a context window of 16,384 tokens with a sliding window",
        "43/43 layers to GPU): 27.63 tokens per second\nllama-2-13b-chat.ggmlv3.q8_0.bin",
        "13B, quantised ... RAM allocation 7.1 GB",
    ],
)
def test_a_quote_matches_despite_punctuation_and_ellipses(quote):
    page = (
        "Grouped Query Attention , a context window of 16,384 tokens with a sliding window "
        "attention, and was trained using the Fill-in-the-Middle objective.\n"
        "13B, quantised to 4 bits, llama-2-13b.bin RAM allocation 7.1 GB\n"
        "- llama-2-13b-chat.ggmlv3.q4_0.bin (43/43 layers to GPU): 27.63 tokens per second\n"
        "- llama-2-13b-chat.ggmlv3.q8_0.bin (43/43 layers to GPU): 19.94 tokens per second"
    )

    assert pipeline._quoted(quote, page)


@pytest.mark.parametrize(
    "quote",
    [
        "a context window of 32,768 tokens",
        # Fragments out of order.
        "RAM allocation 7.1 GB ... 13B, quantised",
        # A word that only appears inside a longer one.
        "4 bit",
        "",
        "...",
    ],
)
def test_a_quote_not_on_the_page_is_refused(quote):
    page = "13B, quantised to 4 bits, RAM allocation 7.1 GB. A context window of 16,384 tokens."

    assert not pipeline._quoted(quote, page)


def test_a_run_with_no_evidence_writes_nothing(settings, monkeypatch):
    # No outline or write replies: asking for either would exhaust the fake and fail the test.
    fake_models(
        monkeypatch,
        {
            "brief": [{"brief": "b", "assumptions": []}],
            "plan": [{"queries": ["q"]}],
            "intake": [{"findings": []}],
        },
    )
    hits = [{"url": "https://a", "title": "A", "description": "nothing", "snippets": []}]
    monkeypatch.setattr(
        pipeline, "build_research_tools", lambda settings: (None, lambda q, exclude_urls: hits)
    )
    monkeypatch.setattr(pipeline, "fetch_text", lambda url: "nothing relevant")
    stream = Stream()

    result = pipeline.run_research(settings, ResearchRequest(question="q", max_rounds=1), stream)

    assert result.report == pipeline.NO_EVIDENCE
    assert result.citations == []
    assert stream.events[-1] == ("write", "no evidence found, so nothing to write")


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


def test_a_sentence_keeps_the_markers_after_its_full_stop():
    line = "Qwen fits in 24 GB [1]. It runs at 30 tok/s, e.g. on a 3090. [2][3] Unsure."

    assert verify.sentences(line) == [
        "Qwen fits in 24 GB [1].",
        "It runs at 30 tok/s, e.g. on a 3090. [2][3]",
        "Unsure.",
    ]


def test_verification_removes_only_unsupported_cited_sentences():
    claims = {1: claim(1, "https://a"), 2: claim(2, "https://b")}
    text = "\n".join(
        [
            "Intro without citation.",
            "Supported [1]. Unsupported [2]. Invented [9].",
            "- Only unsupported [2].",
            "- Kept item [1].",
            "| Model | VRAM [2] |",
            "### Heading [2]",
        ]
    )
    asked = []

    def check(sentence, evidence):
        asked.append((sentence, [c.id for c in evidence]))
        return (1 in [c.id for c in evidence], "no support")

    kept, removed = verify.verify("Findings", text, claims, check)

    assert kept == "\n".join(
        [
            "Intro without citation.",
            "Supported [1].",
            "- Kept item [1].",
            "| Model | VRAM [2] |",
            "### Heading [2]",
        ]
    )
    assert [(r.sentence, r.reason) for r in removed] == [
        ("Unsupported [2].", "no support"),
        ("Invented [9].", "cites no claim the research gathered"),
        ("Only unsupported [2].", "no support"),
    ]
    # An invented id costs no model call.
    assert [sentence for sentence, _ in asked] == [
        "Supported [1].",
        "Unsupported [2].",
        "Only unsupported [2].",
        "Kept item [1].",
    ]
    assert verify.count(text) == 5


def test_an_unreadable_verdict_keeps_the_sentence(monkeypatch):
    model = DummyLM([{"nonsense": "x"}] * 4)
    stream = Stream()

    sections, removed = pipeline._verify(
        model, [("S", "A fact [1].")], {1: claim(1, "https://a")}, stream
    )

    assert sections == [("S", "A fact [1].")]
    assert removed == []
