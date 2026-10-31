"""The deep research pipeline, with every model call, search and page faked."""

from __future__ import annotations

import contextlib

import httpx
import pytest
from dspy.utils.dummies import DummyLM

from kvasir.config import Settings
from kvasir.deep import fetch, pipeline, verify
from kvasir.deep.fetch import FetchError, extract, split
from kvasir.deep.render import matrix, render
from kvasir.deep.store import Claim, Question
from kvasir.models import (
    Cell,
    Clarification,
    Criterion,
    RemovedSentence,
    ResearchRequest,
    Row,
)

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


def test_extract_keeps_the_text_and_drops_page_furniture():
    assert extract(PAGE) == "The stone\nIt was found in 1904 near Kvasir."


def test_split_cuts_between_lines_and_hard_cuts_a_long_line():
    assert split("aaa\nbbb\ncc", size=7) == ["aaa\nbbb", "cc"]
    assert split("abcdefghij", size=4) == ["abcd", "efgh", "ij"]


def pdf(*lines: str) -> bytes:
    """A one-page PDF showing `lines` one under the other."""
    shown = " 0 -14 Td ".join(f"({line}) Tj" for line in lines)
    stream = f"BT /F1 12 Tf 72 720 Td {shown} ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def serve(monkeypatch, content_type, body):
    """Answer every fetch with `body`."""

    def stream(method, url, **kwargs):
        headers = {"content-type": content_type} if content_type else {}
        return contextlib.nullcontext(
            httpx.Response(200, headers=headers, content=body, request=httpx.Request(method, url))
        )

    monkeypatch.setattr(fetch.httpx, "stream", stream)


@pytest.mark.parametrize("content_type", ["application/pdf", "application/octet-stream", None])
def test_a_pdf_is_read_by_its_type_or_its_header(monkeypatch, content_type):
    serve(monkeypatch, content_type, pdf("Kvasir measured the compu-", "tation in 1904."))

    text = fetch.fetch_text("https://example.org/a")

    assert text == "Kvasir measured the computation in 1904."
    assert pipeline._quoted("measured the computation", text)


def test_an_unreadable_pdf_is_refused(monkeypatch):
    serve(monkeypatch, "application/pdf", pdf("Kvasir")[:40])
    with pytest.raises(FetchError, match="unreadable PDF"):
        fetch.fetch_text("https://example.org/a.pdf")


def test_a_pdf_without_text_is_refused(monkeypatch):
    serve(monkeypatch, "application/pdf", pdf())
    with pytest.raises(FetchError, match="no text layer"):
        fetch.fetch_text("https://example.org/a.pdf")


@pytest.mark.parametrize(
    ("content_type", "body"), [("image/png", b"\x89PNG"), ("application/octet-stream", b"PK")]
)
def test_a_page_that_is_neither_html_nor_pdf_is_refused(monkeypatch, content_type, body):
    serve(monkeypatch, content_type, body)
    with pytest.raises(FetchError, match="not HTML or PDF"):
        fetch.fetch_text("https://example.org/a")


def test_a_download_stops_at_the_byte_cap(monkeypatch):
    serve(monkeypatch, "text/html", b"<p>" + b"a" * 100 + b"</p>")
    monkeypatch.setattr(fetch, "MAX_BYTES", 10)

    assert fetch.fetch_text("https://example.org/a") == "aaaaaaa"


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
            "brief": [
                {
                    "brief": "When was the stone found?",
                    "assumptions": ["any source"],
                    "output": "report",
                    "criteria": [],
                    "entities": [],
                }
            ],
            "decompose": [{"questions": ["When was the stone found?"]}],
            # The question's search, then the repair's four for the removed sentence, which find
            # only pages already read.
            "plan": [{"query": "kvasir stone found"}] + [{"query": f"gold {n}"} for n in range(4)],
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
            "check": [{"status": "answered", "claims": [1, 7], "missing": "", "follow_ups": []}],
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
        pipeline, "build_research_tools", lambda settings: (Encoder(), Search(hits))
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

    # The paraphrased "solid gold" quote is on no page, so it is not evidence. The one question is
    # answered after one search, so the run asks for no other query.
    assert [(c.id, c.text, c.url) for c in result.claims] == [(1, "Found in 1904", "https://a")]
    assert result.report == "## Findings\n\nIt was found in 1904 [1]."
    assert [(r.sentence, r.reason) for r in result.removed] == [
        ("It is made of gold [1].", "the quote says nothing about gold")
    ]
    assert [(c.index, c.url, c.snippet) for c in result.citations] == [
        (1, "https://a", "found in   1904")
    ]
    assert result.assumptions == ["any source"]
    assert [(q.text, q.status, q.claims, q.queries) for q in result.questions] == [
        ("When was the stone found?", "answered", [1], ["kvasir stone found"])
    ]
    assert [stage for stage, _ in stream.events][:2] == ["brief", "research"]
    # One event per piece read, each naming its page, the snippet fallback included.
    assert ("research", "search 1: page 1/2, part 1/1: A") in stream.events
    assert ("research", "search 1: page 2/2, part 1/1: B") in stream.events
    assert (
        "research",
        "search 1: 1 new claims, 1 in all; When was the stone found?: answered",
    ) in stream.events
    assert ("verify", "removed 1 of 2 cited sentences") in stream.events
    assert stream.events[-1] == ("verify", "found support for 0 of the sections they came from")


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
            "brief": [
                {
                    "brief": "b",
                    "assumptions": [],
                    "output": "report",
                    "criteria": [],
                    "entities": [],
                }
            ],
            "decompose": [{"questions": ["q?"]}],
            "plan": [{"query": "q"}],
            "intake": [{"findings": []}],
        },
    )
    hits = [{"url": "https://a", "title": "A", "description": "nothing", "snippets": []}]
    monkeypatch.setattr(
        pipeline, "build_research_tools", lambda settings: (Encoder(), Search(hits))
    )
    monkeypatch.setattr(pipeline, "fetch_text", lambda url: "nothing relevant")
    stream = Stream()

    result = pipeline.run_research(settings, ResearchRequest(question="q", max_searches=1), stream)

    assert result.report == pipeline.NO_EVIDENCE
    assert result.citations == []
    assert [(q.text, q.status) for q in result.questions] == [("q?", "open")]
    assert stream.events[-1] == ("write", "no evidence found, so nothing to write")


def test_unreadable_claims_cost_the_piece_not_the_run(monkeypatch):
    model = DummyLM([{"nonsense": "x"}] * 4)
    hit = {"url": "https://a", "title": "A"}

    assert pipeline._intake(model, "brief", "question", hit, "text", 1, 1) == []


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


def test_a_ranking_run_ranks_each_criterion_twice_and_tables_the_cells(settings, monkeypatch):
    fake_models(
        monkeypatch,
        {
            # The question names the options, so they are not chosen again; a name given twice is
            # kept once, in its first spelling.
            "brief": [
                {
                    "brief": "Which is fastest?",
                    "assumptions": [],
                    "output": "report",
                    "criteria": [],
                    "entities": ["Alpha", "Beta", "alpha "],
                }
            ],
            # The background searches, then the question's, then the empty cell's.
            "plan": [
                {"queries": ["fast models"]},
                {"query": "alpha beta speed"},
                {"query": "alpha cost"},
            ],
            "decompose": [{"questions": ["How fast is each?"]}],
            # Out of range weights are clamped to 1 to 3.
            "criteria": [
                {"criteria": [{"name": "speed", "weight": 3}, {"name": "cost", "weight": 0}]}
            ],
            "intake": [
                {
                    "findings": [
                        {
                            "claim": "Alpha: 50 tok/s",
                            "quote": "Alpha runs at 50",
                            "confidence": "high",
                        },
                        {
                            "claim": "Beta: 20 tok/s",
                            "quote": "Beta runs at 20",
                            "confidence": "high",
                        },
                    ]
                }
            ],
            "check": [{"status": "answered", "claims": [1, 2], "missing": "", "follow_ups": []}],
            # Alpha on speed and cost, Beta on speed and cost. The search for Alpha's empty cost
            # cell finds only the page already read, so the cell is not assessed again.
            "assess": [
                {"finding": "50 tok/s", "claims": [1]},
                {"finding": "", "claims": []},
                # 9 does not exist, and 1 is about Alpha: neither is kept.
                {"finding": "20 tok/s", "claims": [1, 2, 9]},
                {"finding": "cheap", "claims": [2]},
            ],
            # Speed twice, the second pass reading the options in reverse. Only Beta has a cost
            # finding, so cost needs no judge: Beta is first and Alpha, with none, last.
            "judge": [{"ranking": ["Alpha", "Beta"]}, {"ranking": ["1. alpha"]}],
            "outline": [{"sections": ["Alpha"]}],
            "write": [{"text": "Alpha is the fastest [1]."}],
            "verify": [{"supported": True, "reason": "the quote gives the speed"}],
        },
    )
    hits = [{"url": "https://a", "title": "A", "description": "speeds", "snippets": []}]
    monkeypatch.setattr(
        pipeline, "build_research_tools", lambda settings: (Encoder(), Search(hits))
    )
    monkeypatch.setattr(
        pipeline, "fetch_text", lambda url: "Alpha runs at 50 tok/s. Beta runs at 20 tok/s."
    )
    stream = Stream()

    result = pipeline.run_research(
        settings,
        ResearchRequest(
            question="Which is faster, Alpha or Beta?", output="ranking", max_attempts=1
        ),
        stream,
    )

    assert result.output == "ranking"
    assert [(c.name, c.weight) for c in result.criteria] == [("speed", 3), ("cost", 1)]
    # Alpha: (3 x 1 + 1 x 2) / 4 = 1.25. Beta: (3 x 2 + 1 x 1) / 4 = 1.75.
    assert [(row.entity, row.rank) for row in result.matrix] == [("Alpha", 1), ("Beta", 2)]
    assert [(c.finding, c.claims, c.rank) for c in result.matrix[1].cells] == [
        ("20 tok/s", [2], 2.0),
        ("cheap", [2], 1),
    ]
    assert result.matrix[0].cells[1].rank == 2
    # Both claims come from one page, so both cite source 1.
    assert result.report.startswith(
        "## Ranking\n\n| Rank | Option | speed | cost |\n| --- | --- | --- | --- |\n"
        "| 1 | Alpha | 50 tok/s [1] | – |\n"
        "| 2 | Beta | 20 tok/s [1] | cheap [1] |\n"
    )
    assert result.report.endswith("## Alpha\n\nAlpha is the fastest [1].")
    # The page was read for the first question, so the search for the empty cell finds nothing.
    assert ("assess", "filled 0 of 1 empty cells") in stream.events
    assert ("assess", "ranked: Alpha, Beta") in stream.events


def test_a_ranked_name_matches_without_its_number_or_case():
    assert pipeline._ordered(
        ["2) beta", "Gamma", "BETA", "1. Alpha"], ["Alpha", "Beta", "Delta"]
    ) == [
        "Beta",
        "Alpha",
        "Delta",
    ]


def test_a_table_has_no_rank_and_keeps_its_cells_whole():
    rows = [
        Row(
            entity="A | B",
            cells=[Cell(criterion="licence", finding="MIT\nor Apache", claims=[4, 5])],
        )
    ]

    assert matrix(rows, [Criterion(name="licence")]) == (
        "| Option | licence |\n| --- | --- |\n| A / B | MIT or Apache [4][5] |\n\n"
        "A dash means no claim spoke to it."
    )


@pytest.mark.parametrize(
    "entity, text, named",
    [
        ("Qwen3-Coder-30B-A3B", "Qwen3 Coder 30B runs at 69 tok/s", True),
        ("Claude Opus 5.5", "Qwen3-Coder needs 17 GB at 4-bit", False),
        # A word inside a longer one is not the name.
        ("Phi 4", "Phi 14B fits in 12 GB", False),
    ],
)
def test_a_cited_claim_must_name_the_option(entity, text, named):
    assert pipeline._names(entity, text) is named


def test_criteria_and_options_named_in_the_question_are_final(settings, monkeypatch):
    # No criteria, entities or second plan reply: choosing either again would exhaust the fakes.
    fake_models(
        monkeypatch,
        {
            "brief": [
                {
                    "brief": "Rank phones on price and privacy.",
                    "assumptions": [],
                    "output": "ranking",
                    "criteria": ["price", "privacy"],
                    # Galaxy is from the model's memory, not the question.
                    "entities": ["Pixel", "Galaxy S30", "iPhone"],
                }
            ],
            "decompose": [{"questions": ["q?"]}],
            "plan": [{"query": "q"}],
            "intake": [{"findings": []}],
        },
    )
    hits = [{"url": "https://a", "title": "A", "description": "nothing", "snippets": []}]
    monkeypatch.setattr(
        pipeline, "build_research_tools", lambda settings: (Encoder(), Search(hits))
    )
    monkeypatch.setattr(pipeline, "fetch_text", lambda url: "nothing relevant")
    stream = Stream()

    pipeline.run_research(
        settings,
        ResearchRequest(
            question="Rank the Pixel and the iPhone on price and privacy", max_searches=1
        ),
        stream,
    )

    assert [e for e in stream.events if e[0] == "background"] == [
        ("background", "comparing 2 options on 2 criteria: Pixel, iPhone")
    ]


def test_a_chosen_option_the_search_never_mentions_is_dropped(settings, monkeypatch):
    fake_models(
        monkeypatch,
        {
            "brief": [
                {
                    "brief": "Rank phones on price.",
                    "assumptions": [],
                    "output": "ranking",
                    "criteria": ["price"],
                    "entities": [],
                }
            ],
            # The background searches, then the question's.
            "plan": [{"queries": ["best phones"]}, {"query": "q"}],
            "decompose": [{"questions": ["q?"]}],
            "entities": [{"entities": ["Pixel 10", "Phantom 9"]}],
            "intake": [{"findings": []}],
        },
    )
    hits = [{"url": "https://a", "title": "A", "description": "The Pixel 10 leads", "snippets": []}]
    monkeypatch.setattr(
        pipeline, "build_research_tools", lambda settings: (Encoder(), Search(hits))
    )
    monkeypatch.setattr(pipeline, "fetch_text", lambda url: "nothing relevant")
    stream = Stream()

    pipeline.run_research(
        settings, ResearchRequest(question="Rank phones on price", max_searches=1), stream
    )

    assert ("background", "comparing 1 options on 1 criteria: Pixel 10") in stream.events


def test_a_run_whose_searches_all_fail_stops_with_the_reason(settings, monkeypatch):
    fake_models(
        monkeypatch,
        {
            "brief": [
                {
                    "brief": "b",
                    "assumptions": [],
                    "output": "report",
                    "criteria": [],
                    "entities": [],
                }
            ],
            "decompose": [{"questions": ["q?"]}],
            "plan": [{"query": "q1"}, {"query": "q2"}, {"query": "q3"}],
        },
    )
    failing = Search([], ["brave: Suspended: too many requests", "duckduckgo: CAPTCHA"])
    monkeypatch.setattr(pipeline, "build_research_tools", lambda settings: (Encoder(), failing))

    with pytest.raises(pipeline.SearchUnavailable) as raised:
        pipeline.run_research(settings, ResearchRequest(question="q"), Stream())

    # Each engine once, though every search reported it.
    assert str(raised.value) == (
        "3 searches in a row failed: brave: Suspended: too many requests; duckduckgo: CAPTCHA"
    )


class Pages:
    """SearXNG answering each query with one page never seen before."""

    failures: list[str] = []

    def __init__(self):
        self.queries = []

    def __call__(self, query, exclude_urls):
        self.queries.append(query)
        url = f"https://example.org/{len(self.queries)}"
        return [{"url": url, "title": url, "description": "", "snippets": []}]


def loop(monkeypatch, questions, found, judge, budget=16, max_attempts=4, search=None):
    """Run `_research` over `questions`, the model writing `query N` and reading `found`.

    `found` maps a query number to the claim texts its page yields. Returns the search, the shapes
    the model was asked for and the claims gathered.
    """
    shapes = []

    def predict(model, signature, **inputs):
        assert signature is pipeline.SearchFor
        shapes.append(inputs["shape"])
        return type("Prediction", (), {"query": f"query {len(shapes)}"})

    def read(model, encoder, brief, question, hits, first_id, step, label, stream):
        texts = found.get(len(shapes), []) if hits else []
        return [claim(first_id + i, hits[0]["url"]) for i, _ in enumerate(texts)]

    monkeypatch.setattr(pipeline, "_predict", predict)
    monkeypatch.setattr(pipeline, "_read", read)
    search = search or Pages()
    claims: list[Claim] = []
    searches = pipeline._research(
        {"plan": None, "intake": None},
        Encoder(),
        search,
        "2026-11-01",
        "brief",
        questions,
        claims,
        set(),
        budget,
        1,
        max_attempts,
        judge,
        0,
        Stream(),
    )
    return searches, shapes, claims


def test_a_question_nothing_answers_is_tried_in_every_shape_then_given_up(monkeypatch):
    question = Question(id=1, text="When?")

    searches, shapes, _ = loop(monkeypatch, [question], {}, judge=lambda q: None)

    assert searches == 4
    assert shapes == list(pipeline.QUERY_SHAPES)
    assert question.queries == ["query 1", "query 2", "query 3", "query 4"]
    assert question.status == "unresolved"


def test_the_budget_caps_the_searches(monkeypatch):
    questions = [Question(id=1, text="a"), Question(id=2, text="b")]

    searches, _, _ = loop(monkeypatch, questions, {}, judge=lambda q: None, budget=3)

    assert searches == 3
    # Each gets a plain search before either gets a narrower one.
    assert [q.attempts for q in questions] == [2, 1]


def test_a_question_another_search_answered_costs_no_search(monkeypatch):
    first, second = Question(id=1, text="a"), Question(id=2, text="b")
    judged = []

    def judge(question):
        judged.append(question.id)
        question.judged_at = 1
        question.status = "answered"

    searches, _, _ = loop(monkeypatch, [first, second], {1: ["a and b"]}, judge)

    assert searches == 1
    assert judged == [1, 2]
    assert second.queries == []


def test_a_failed_search_spends_the_budget_but_not_the_attempt(monkeypatch):
    class Flaky(Pages):
        def __call__(self, query, exclude_urls):
            self.failures = ["brave: CAPTCHA"] if len(self.queries) < 2 else []
            hits = super().__call__(query, exclude_urls)
            return [] if self.failures else hits

    question = Question(id=1, text="When?")

    searches, shapes, _ = loop(
        monkeypatch, [question], {}, judge=lambda q: None, budget=3, search=Flaky()
    )

    assert searches == 3
    # Two failures, then the one search answered: the plain shape each time.
    assert shapes == [pipeline.QUERY_SHAPES[0]] * 3
    assert question.queries == ["query 3"]


def test_the_judge_counts_only_claims_it_was_shown(monkeypatch):
    claims = [claim(1, "https://a"), claim(2, "https://b")]
    question = Question(id=1, text="When?")
    model = DummyLM(
        [{"status": "answered", "claims": [2, 9], "missing": "", "follow_ups": ["Why?"]}]
    )

    asked = pipeline._check(model, Encoder(), "brief", claims, question)

    assert (question.status, question.claims, question.judged_at) == ("answered", [2], 2)
    assert asked == ["Why?"]
    # Nothing new among the closest claims: no call, which would exhaust the fake.
    assert pipeline._check(model, Encoder(), "brief", claims, question) == []


def test_an_answer_resting_on_no_shown_claim_is_open(monkeypatch):
    question = Question(id=1, text="When?")
    model = DummyLM(
        [{"status": "answered", "claims": [9], "missing": "the year", "follow_ups": ["Why?"]}]
    )

    asked = pipeline._check(model, Encoder(), "brief", [claim(1, "https://a")], question)

    assert (question.status, question.claims, question.missing) == ("open", [], "the year")
    assert asked == []


def test_a_repeated_question_is_not_added(monkeypatch):
    class Words:
        def encode(self, texts):
            return [[1.0, 0.0] if "year" in text else [0.0, 1.0] for text in texts]

    ledger = [Question(id=1, text="Which year?")]
    parent = ledger[0]

    pipeline._add_questions(
        Words(), ledger, ["The year it was found?", "Who found it?", " "], parent
    )

    assert [(q.id, q.text, q.parent, q.depth) for q in ledger] == [
        (1, "Which year?", None, 0),
        (2, "Who found it?", 1, 1),
    ]


def test_the_report_ends_with_what_the_research_did_not_settle():
    searched = ["q"]
    ledger = [
        Question(id=1, text="When?", status="answered", queries=searched),
        Question(id=2, text="Who?", status="unresolved", queries=searched),
        Question(id=3, text="Why?", status="weak", queries=searched),
        Question(id=4, text="Where?", status="contradicted", queries=searched),
        Question(id=5, text="cost of Alpha", status="open", cell=("Alpha", "cost")),
        Question(id=6, text="How?", status="open"),
        # A follow-up the budget never reached.
        Question(id=7, text="How exactly?", status="open", parent=1, depth=1),
    ]

    assert pipeline._open_questions(ledger) == (
        "- Who? (not found)\n- Why? (partly answered)\n- Where? (sources disagree)\n"
        "- How? (not searched)"
    )


def test_a_section_whose_removed_sentence_the_search_supports_is_written_again():
    claims = [claim(1, "https://a")]
    removed = [
        RemovedSentence(section="Findings", sentence="It is gold [1].", reason="no gold"),
        RemovedSentence(section="Ranking", sentence="Row [1].", reason="never asked"),
    ]
    searched = []

    def research(questions, budget, judge):
        searched.append([(q.text, q.missing) for q in questions])
        claims.append(claim(2, "https://b", quote="solid gold"))
        questions[0].status, questions[0].claims = "answered", [2]

    models = {
        "write": DummyLM([{"text": "It is gold [2]. It shines [2]."}]),
        "verify": DummyLM(
            [{"supported": True, "reason": "says gold"}, {"supported": False, "reason": "no shine"}]
        ),
    }

    sections, left = pipeline._repair(
        models,
        Encoder(),
        "brief",
        ["Findings"],
        claims,
        [("Ranking", "| table |"), ("Findings", "Found in 1904 [1].")],
        removed,
        research,
        lambda q: None,
        Stream(),
    )

    # Only sentences from written sections: the ranking table is built, not written.
    assert searched == [[("It is gold.", "no gold")]]
    assert sections == [("Ranking", "| table |"), ("Findings", "It is gold [2].")]
    assert [(r.section, r.sentence) for r in left] == [
        ("Ranking", "Row [1]."),
        ("Findings", "It shines [2]."),
    ]
