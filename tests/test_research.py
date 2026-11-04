"""The steps every research method shares, with every model call, search and page faked."""

from __future__ import annotations

import contextlib
import dataclasses

import httpx
import numpy as np
import pytest
from dspy.utils.dummies import DummyLM

from fakes import PAGE, Encoder, Stream, Tiers, Vectors, claim, page, research_settings
from kvasir import runners
from kvasir.methods.ledger.frontier import Frontier
from kvasir.methods.ledger.loop import purge
from kvasir.methods.ledger.questions import Question
from kvasir.research import fetch, verify, write
from kvasir.research.clarify import clarify
from kvasir.research.corpus import RECALL_MIN, RECALL_PER_SITE, RECALL_PIECES, Corpus
from kvasir.research.fetch import FetchError, extract, split
from kvasir.research.read import (
    GAP,
    PAGE_CHARACTERS,
    fetch_all,
    intake,
    pack,
    page_characters,
    recall,
    relevant_pieces,
)
from kvasir.research.render import matrix, render
from kvasir.research.style import clean
from kvasir.research.text import misleading, names, quoted, with_table_header
from kvasir.schemas.results import Cell, Criterion, Row


@pytest.fixture
def settings():
    return research_settings()


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

    text = fetch.fetch_page("https://example.org/a").text

    assert text == "Kvasir measured the computation in 1904."
    assert quoted("measured the computation", text)


def test_an_unreadable_pdf_is_refused(monkeypatch):
    serve(monkeypatch, "application/pdf", pdf("Kvasir")[:40])
    with pytest.raises(FetchError, match="unreadable PDF"):
        fetch.fetch_page("https://example.org/a.pdf")


def test_a_pdf_without_text_is_refused(monkeypatch):
    serve(monkeypatch, "application/pdf", pdf())
    with pytest.raises(FetchError, match="no text layer"):
        fetch.fetch_page("https://example.org/a.pdf")


@pytest.mark.parametrize(
    ("content_type", "body"), [("image/png", b"\x89PNG"), ("application/octet-stream", b"PK")]
)
def test_a_page_that_is_neither_html_nor_pdf_is_refused(monkeypatch, content_type, body):
    serve(monkeypatch, content_type, body)
    with pytest.raises(FetchError, match="not HTML or PDF"):
        fetch.fetch_page("https://example.org/a")


def test_a_download_stops_at_the_byte_cap(monkeypatch):
    serve(monkeypatch, "text/html", b"<body><p>" + b"a" * 100 + b"</p></body>")
    monkeypatch.setattr(fetch, "MAX_BYTES", 60)

    assert fetch.fetch_page("https://example.org/a").text == "a" * 51


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

    assert quoted(quote, page)


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

    assert not quoted(quote, page)


@pytest.mark.parametrize(
    "page",
    [
        "It never grows in the dark.",
        "It isn't grows in the dark.",
        "Unless grows in the dark.",
    ],
)
def test_a_quote_right_after_a_negation_is_misleading(page):
    assert quoted("grows in the dark", page)

    assert misleading("grows in the dark", page)


def test_an_ellipsis_over_a_negator_is_misleading():
    assert misleading("the drug ... works", "the drug does not work works")
    assert misleading("it ... grows", "it is only said to grows")
    assert misleading("this ... helps", "this isn't known to helps")


@pytest.mark.parametrize(
    ("quote", "page"),
    [
        ("grows in the dark", "It grows in the dark."),
        ("the drug ... works", "The drug often really works."),
        # A "t" that is not a contraction.
        ("cells grow", "T cells grow."),
        ("cells divide", "Seen in T cells divide."),
        # The negation is after the quote, or in another sentence's end.
        ("the drug works", "The drug works, not always."),
        ("cats grow", "It is not. Cats grow."),
    ],
)
def test_an_ordinary_quote_is_not_misleading(quote, page):
    assert quoted(quote, page)

    assert not misleading(quote, page)


def snippet_hits(monkeypatch):
    def fetch_page(url):
        if url == "https://down":
            raise FetchError("down")
        return page("the page")

    monkeypatch.setattr("kvasir.research.read.fetch_page", fetch_page)
    return [
        {"url": "https://down", "title": "D", "description": "the snippet"},
        {"url": "https://up", "title": "U", "description": "the snippet"},
    ]


def test_fetch_all_tells_which_pages_fell_back_to_the_snippet(monkeypatch):
    hits = snippet_hits(monkeypatch)

    assert list(fetch_all(hits)) == ["the snippet", page("the page")]


@pytest.mark.parametrize("from_snippet", [True, False])
def test_a_claim_knows_whether_it_was_read_from_a_snippet(from_snippet):
    model = DummyLM(
        [{"findings": [{"claim": "Found", "quote": "found in 1904", "confidence": "high"}]}]
    )
    hit = {"url": "https://a", "title": "A"}

    claims = intake(model, "b", "q", hit, "It was found in 1904.", 1, 1, from_snippet)

    assert [c.from_snippet for c in claims] == [from_snippet]


def test_intake_drops_a_quote_a_negation_turns_around():
    model = DummyLM(
        [
            {
                "findings": [
                    {"claim": "Grows", "quote": "grows in the dark", "confidence": "high"},
                    {"claim": "Drug works", "quote": "the drug ... works", "confidence": "high"},
                    {"claim": "Found", "quote": "found in 1904", "confidence": "high"},
                ]
            }
        ]
    )
    hit = {"url": "https://a", "title": "A"}
    piece = "It never grows in the dark. The drug is not known to works. It was found in 1904."

    assert [c.text for c in intake(model, "b", "q", hit, piece, 1, 1)] == ["Found"]


TABLE = (
    "Benchmarks\n\n| Metric | WasmEdge | Wasmtime | Wasmer |\n|---|---|---|---|\n"
    "| Cold start | 1.5ms | 3ms | 2ms |\n| Memory | 9MB | 12MB | 11MB |\n\nAfter the table."
)


def test_a_table_row_quote_gets_its_header():
    quote = with_table_header("Cold start 1.5ms 3ms 2ms", TABLE)

    assert quote == "| Metric | WasmEdge | Wasmtime | Wasmer | ... Cold start 1.5ms 3ms 2ms"
    assert quoted(quote, TABLE)


def test_a_quote_outside_a_table_or_with_its_header_is_unchanged():
    with_header = "Metric WasmEdge Wasmtime Wasmer"

    assert with_table_header("After the table", TABLE) == "After the table"
    assert with_table_header(with_header, TABLE) == with_header


def test_intake_stores_a_table_row_with_its_header():
    model = DummyLM(
        [
            {
                "findings": [
                    {
                        "claim": "Cold start",
                        "quote": "Cold start 1.5ms 3ms 2ms",
                        "confidence": "high",
                    }
                ]
            }
        ]
    )
    hit = {"url": "https://a", "title": "A"}

    [claim] = intake(model, "b", "q", hit, TABLE, 1, 1)

    assert claim.quote.startswith("| Metric | WasmEdge")


def test_unreadable_claims_cost_the_piece_not_the_run(monkeypatch):
    model = DummyLM([{"nonsense": "x"}] * 4)
    hit = {"url": "https://a", "title": "A"}

    assert intake(model, "brief", "question", hit, "text", 1, 1) == []


def test_an_unreadable_section_is_empty_rather_than_the_end_of_the_run():
    model = DummyLM([{"nonsense": "x"}] * 4)

    assert write.section(model, Encoder(), "brief", ["A"], "A", [], [claim(1, "https://a")]) == ""


def test_a_confidence_of_moderate_is_medium():
    model = DummyLM(
        [{"findings": [{"claim": "Found", "quote": "found in 1904", "confidence": "moderate"}]}]
    )
    hit = {"url": "https://a", "title": "A"}

    claims = intake(model, "b", "q", hit, "It was found in 1904.", 1, 1)

    assert [c.confidence for c in claims] == ["medium"]


def test_a_finding_that_fails_validation_costs_the_piece_not_the_run():
    model = DummyLM(
        [{"findings": [{"claim": "Found", "quote": "found in 1904", "confidence": "certain"}]}] * 4
    )
    hit = {"url": "https://a", "title": "A"}

    assert intake(model, "b", "q", hit, "It was found in 1904.", 1, 1) == []


def test_clarify_asks_at_most_three_questions(settings, monkeypatch):
    model = DummyLM([{"questions": ["a?", "b?", "c?", "d?"]}])
    monkeypatch.setattr(
        "kvasir.research.clarify.build_research_models", lambda *args: {"clarify": model}
    )

    assert clarify(settings, "best battery makers") == ["a?", "b?", "c?"]


def test_a_sentence_keeps_the_markers_after_its_full_stop():
    line = "Qwen fits in 24 GB [1]. It runs at 30 tok/s, e.g. on a 3090. [2][3] Unsure."

    assert verify.sentences(line) == [
        "Qwen fits in 24 GB [1].",
        "It runs at 30 tok/s, e.g. on a 3090. [2][3]",
        "Unsure.",
    ]


def test_editing_decides_only_cited_sentences():
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

    def decide(sentence):
        asked.append(sentence)
        return "" if sentence.startswith(("Unsupported", "Invented", "Only")) else sentence

    kept = verify.edit(text, decide)

    assert kept == "\n".join(
        [
            "Intro without citation.",
            "Supported [1].",
            "- Kept item [1].",
            "| Model | VRAM [2] |",
            "### Heading [2]",
        ]
    )
    assert asked == [
        "Supported [1].",
        "Unsupported [2].",
        "Invented [9].",
        "Only unsupported [2].",
        "Kept item [1].",
    ]


def test_an_unreadable_verdict_goes_to_the_reason_model(monkeypatch):
    fast, reason = DummyLM([{"nonsense": "x"}] * 4), DummyLM([{"nonsense": "x"}] * 4)
    models = {"verify": fast, "escalate": reason}

    checked = verify.report(models, [("S", "A fact [1].")], {1: claim(1, "https://a")}, Stream())

    assert checked.sections == [("S", f"A fact [1]. {verify.UNVERIFIED}")]
    assert checked.removed == []


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
    assert names(entity, text) is named


def test_a_call_reads_what_the_context_holds_once_the_prompt_and_answer_are_out():
    # (16384 - 1000 prompt - 4096 answer) tokens, at 3.5 characters each, less a tenth.
    assert page_characters(16384) == 35557
    assert page_characters(32768) > 2 * page_characters(16384)


def test_pieces_are_packed_in_order_into_texts_that_fit():
    assert pack(["aaaa", "bb", "cccccc", "d"], 6 + len(GAP) + 1) == [
        f"aaaa{GAP}bb",
        f"cccccc{GAP}d",
    ]
    # Too long to share, a piece is read alone.
    assert pack(["a" * 20, "b"], 10) == ["a" * 20, "b"]


def test_a_report_opens_with_its_summary_then_contents_linking_each_heading():
    claims = {1: claim(1, "https://a"), 2: claim(2, "https://b")}
    sections = [
        ("Trade & war", "### Ships\n\nShips sank [2].\n\n### Ports [2]\n\nPorts burned."),
        ("Trade & war", "Again."),
        ("Drought", "Rain failed [1]."),
    ]

    report, citations = render(sections, claims, "- Rain failed [1].\n- Ships sank [2].")

    assert report.split("\n\n## ")[0] == (
        "> [!SUMMARY] TL;DR\n> - Rain failed [1].\n> - Ships sank [2].\n\n"
        "> [!ABSTRACT] Contents\n"
        "> - [Trade & war](#trade--war)\n"
        ">   - [Ships](#ships)\n"
        ">   - [Ports](#ports)\n"
        "> - [Trade & war](#trade--war-1)\n"
        "> - [Drought](#drought)"
    )
    # Sources are numbered in reading order, the summary first.
    assert [c.url for c in citations] == ["https://a", "https://b"]


def test_a_short_report_has_no_contents():
    report, _ = render([("A", "a"), ("B", "b")], {})

    assert report == "## A\n\na\n\n## B\n\nb"


def test_a_callout_keeps_its_supported_sentences_and_goes_once_it_has_none():
    text = "> [!WARNING] Disputed\n> **Dates** differ [1].\n\n> [!NOTE]\n> Wrong [1].\n\nAfter [1]."

    kept = verify.edit(text, lambda sentence: "" if sentence.startswith("Wrong") else sentence)

    assert kept == "> [!WARNING] Disputed\n> **Dates** differ [1].\n\n\nAfter [1]."


def test_the_style_pass_takes_out_filler_but_not_quotes_or_code():
    text = (
        "Notably, **Ugarit** fell 1190—1185 BC [1]. It is important to note that trade — "
        'the lifeline — was really very fragile. He wrote "very — late" and `a — b`.'
    )

    assert clean(text) == (
        "**Ugarit** fell 1190–1185 BC [1]. Trade, the lifeline, was fragile. "
        'He wrote "very — late" and `a — b`.'
    )


def test_the_closest_pieces_are_kept_up_to_the_page_budget_in_page_order():
    # Four sections, each its own piece; three fit the budget, so the farthest is dropped.
    size = PAGE_CHARACTERS // 3 - 100
    sections = [f"## {name}\n{name[0] * size}" for name in ("Alpha", "Beta", "Gamma", "Delta")]
    text = "# Page\n" + "\n\n".join(sections) + "\n\n## Coda\nshort"
    near = {"q": [1.0, 0.0]}
    for piece in split(text):
        far = piece.startswith("## Beta")
        near[f"T\n{piece}"] = [0.0, 1.0] if far else [1.0, 0.0]
    corpus = Corpus()

    kept = relevant_pieces(corpus, Vectors(near, default=[0.5, 0.5]), "q", "T", page(text))

    assert sum(map(len, kept)) <= PAGE_CHARACTERS
    assert not any(piece.startswith("## Beta") for piece in kept)
    assert kept == [piece for piece in split(text) if piece in kept]
    # The corpus holds every piece, and only the kept ones are marked read.
    assert [p.text for p in corpus.pieces] == split(text)
    assert [p.text for p in corpus.pieces if p.read] == kept


def test_a_page_within_the_budget_is_read_whole():
    text = "# Page\n## One\nfirst\n\n## Two\nsecond"
    corpus = Corpus()

    assert relevant_pieces(corpus, Encoder(), "q", "T", page(text)) == split(text)
    assert all(p.read for p in corpus.pieces)


class Counting(Vectors):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def encode(self, texts, query=False):
        self.calls.append((list(texts), query))
        return super().encode(texts, query)


def test_a_page_is_encoded_once_however_it_is_picked():
    size = PAGE_CHARACTERS // 3 - 100
    text = "# Page\n" + "\n\n".join(
        f"## {n}\n{n[0] * size}" for n in ("Alpha", "Beta", "Gamma", "Delta")
    )
    encoder = Counting({}, default=[1.0, 0.0])

    relevant_pieces(Corpus(), encoder, "q", "T", page(text))

    documents = [texts for texts, query in encoder.calls if not query]
    assert len(documents) == 1
    assert len(documents[0]) == len(split(text))


def corpus_of(vectors, url="https://a.org/x"):
    """A corpus holding one page with a piece per vector, named p0, p1, ... in order."""
    corpus = Corpus()
    texts = [f"p{i}" for i in range(len(vectors))]
    corpus.add(with_url(url), texts, np.array(vectors))
    return corpus


def with_url(url):
    return dataclasses.replace(page("x"), url=url, final_url=url)


QUESTION = Vectors({"q": [1.0, 0.0]})


def test_recall_reads_the_pieces_held_into_claims_and_counts_them_for_their_site():
    corpus = Corpus()
    corpus.add(
        dataclasses.replace(
            page("x"), url="https://a.org/x", final_url="https://www.a.org/x/", title="A"
        ),
        ["It was found in 1904.", "Far from it."],
        np.array([[1.0, 0.0], [0.0, 1.0]]),
    )
    model = DummyLM(
        [{"findings": [{"claim": "Found", "quote": "found in 1904", "confidence": "high"}]}]
    )
    stream = Stream()

    claims = recall(model, QUESTION, "b", "q", corpus, 5, 2, "recall for q", stream)

    assert [(c.id, c.url, c.title, c.step) for c in claims] == [(5, "https://www.a.org/x/", "A", 2)]
    assert corpus.claims == {"a.org": 1}
    assert [piece.read for piece in corpus.pieces] == [True, False]
    assert stream.events == [
        ("research", "recall for q: 1 pieces from 1 pages"),
        ("research", "recall for q: read page 1/1: A"),
        ("research", "recall for q: 1 new claims, 5 in all"),
    ]
    assert stream.fields == [
        {"done": 1, "total": 1, "kind": "recall", "parent": None, "question": None},
        {"done": 1, "total": 1, "kind": "part", "parent": 0, "page": 1},
        {"done": 1, "total": 5, "kind": "result", "parent": 0},
    ]


def test_recall_numbers_its_pages_and_names_its_question_and_parent():
    corpus = Corpus()
    for url in ("https://a.org/x", "https://b.org/y"):
        corpus.add(with_url(url), ["p0", "p1"], np.array([[1.0, 0.0], [1.0, 0.0]]))
    model = DummyLM([{"findings": []}, {"findings": []}])
    stream = Stream()

    recall(
        model,
        QUESTION,
        "b",
        "q",
        corpus,
        1,
        0,
        "recall",
        stream,
        question_id=7,
        parent=3,
        stage="verify",
    )

    assert [
        (f["kind"], f["parent"], f.get("page"), f["done"], f["total"]) for f in stream.fields
    ] == [
        ("recall", 3, None, 3, 2),
        ("part", 0, 1, 1, 2),
        ("part", 0, 2, 2, 2),
        ("result", 0, None, 0, 0),
    ]
    assert stream.fields[0]["question"] == 7
    assert {stage for stage, _ in stream.events} == {"verify"}


def test_recall_with_nothing_held_makes_no_call():
    stream = Stream()

    assert recall(None, QUESTION, "b", "q", Corpus(), 1, 0, "recall for q", stream) == []
    assert stream.events == []


def test_recall_takes_the_pieces_at_or_above_the_threshold_best_first():
    off = [RECALL_MIN - 0.1, (1 - (RECALL_MIN - 0.1) ** 2) ** 0.5]
    corpus = corpus_of([[0.7, 0.714], [1.0, 0.0], off])

    recalled = corpus.recall(QUESTION, "q")

    assert [text for _, text in recalled] == ["p1", "p0"]


def test_recall_stops_at_the_piece_limit():
    corpus = Corpus()
    for i in range(RECALL_PIECES + 1):
        corpus.add(with_url(f"https://s{i}.org"), [f"p{i}"], np.array([[1.0, 0.0]]))

    assert len(corpus.recall(QUESTION, "q")) == RECALL_PIECES


def test_recall_takes_a_site_only_so_many_times():
    corpus = Corpus()
    corpus.add(with_url("https://www.a.org/1"), ["a1", "a2"], np.array([[1.0, 0.0]] * 2))
    corpus.add(with_url("https://a.org/2"), ["a3"], np.array([[1.0, 0.0]]))
    corpus.add(with_url("https://b.org"), ["b1"], np.array([[0.9, 0.1]]))

    recalled = [text for _, text in corpus.recall(QUESTION, "q")]

    assert RECALL_PER_SITE == 2
    assert recalled == ["a1", "a2", "b1"]


def test_a_piece_recalled_or_read_is_not_recalled_again():
    corpus = corpus_of([[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]])
    corpus.pieces[0].read = True

    assert [text for _, text in corpus.recall(QUESTION, "q")] == ["p1", "p2"]
    assert corpus.recall(QUESTION, "q") == []


def test_the_corpus_counts_pages_and_claims_per_site():
    corpus = Corpus()
    corpus.add(with_url("https://www.a.org/1"), ["x"], np.array([[1.0]]))
    corpus.add(with_url("https://a.org/2"), ["y"], np.array([[1.0]]))

    corpus.record([claim(1, "https://a.org/1"), claim(2, "https://b.org")])

    assert corpus.pages == {"a.org": 2}
    assert corpus.claims == {"a.org": 1, "b.org": 1}


def test_a_site_found_avoid_after_it_was_read_is_purged_with_its_corroborations():
    claims = [claim(1, "https://a.org/1"), claim(2, "https://bad.org/2")]
    claims[0].corroborated_by = [2]
    question = Question(id=1, text="q", status="answered", claims=[1, 2])
    stream = Stream()
    corpus = Corpus(sources=Tiers(bad_org="avoid"))

    purge(Encoder(), "brief", None, corpus, [question], claims, stream)

    assert [c.id for c in claims] == [1]
    assert claims[0].corroborated_by == [] and question.claims == [1]
    assert stream.events == [("research", "dropped 1 claims from sites found untrustworthy since")]


def test_nothing_is_purged_without_a_classifier():
    claims = [claim(1, "https://bad.org/1")]
    stream = Stream()

    purge(Encoder(), "brief", Frontier(Encoder(), "brief", claims), Corpus(), [], claims, stream)

    assert len(claims) == 1 and stream.events == []


def test_a_purge_scrubs_the_cells_that_cited_the_dropped_claims():
    claims = [claim(1, "https://a.org/1"), claim(2, "https://bad.org/2")]
    rows = [
        Row(
            entity="X",
            cells=[
                Cell(criterion="c", finding="f", claims=[1, 2]),
                Cell(criterion="d", finding="g", claims=[2]),
            ],
        )
    ]
    sources = Tiers(bad_org="avoid")

    purge(Encoder(), "brief", None, Corpus(sources=sources), [], claims, Stream(), rows)

    assert [(c.finding, c.claims) for c in rows[0].cells] == [("f", [1]), ("", [])]
    # The purge before writing waits for Wikidata; the other lookups do not.
    assert sources.deadlines[-1] > 0
    Corpus(sources=sources).low_trust(claims)
    assert sources.deadlines[-1] == 0


def test_the_storm_retriever_filters_avoid_urls(monkeypatch):
    monkeypatch.setattr(runners, "build_sources", lambda settings: Tiers(bad_org="avoid"))

    retriever = runners._retriever(research_settings(), 3)

    assert not retriever.is_valid_source("https://bad.org/x")
    assert retriever.is_valid_source("https://a.org/x")


def test_one_classifier_is_built_per_process_and_settings(tmp_path):
    def configured(**changes):
        return dataclasses.replace(
            research_settings(),
            data_dir=tmp_path,
            sources=dataclasses.replace(research_settings().sources, enabled=True, **changes),
        )

    assert runners.build_sources(research_settings()) is None
    first = runners.build_sources(configured())
    assert runners.build_sources(configured()) is first
    assert runners.build_sources(configured(wikidata=False)) is not first
