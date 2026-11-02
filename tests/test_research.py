"""The steps every research method shares, with every model call, search and page faked."""

from __future__ import annotations

import contextlib

import httpx
import pytest
from dspy.utils.dummies import DummyLM

from fakes import PAGE, Stream, claim, research_settings
from kvasir.research import fetch, verify
from kvasir.research.clarify import clarify
from kvasir.research.fetch import FetchError, extract, split
from kvasir.research.read import GAP, fetch_all, intake, pack, page_characters
from kvasir.research.render import matrix, render
from kvasir.research.style import clean
from kvasir.research.text import misleading, names, quoted
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

    text = fetch.fetch_text("https://example.org/a")

    assert text == "Kvasir measured the computation in 1904."
    assert quoted("measured the computation", text)


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
        # The negation is after the quote, or in another sentence's end.
        ("the drug works", "The drug works, not always."),
    ],
)
def test_an_ordinary_quote_is_not_misleading(quote, page):
    assert quoted(quote, page)

    assert not misleading(quote, page)


def snippet_hits(monkeypatch):
    def fetch_text(url):
        if url == "https://down":
            raise FetchError("down")
        return "the page"

    monkeypatch.setattr("kvasir.research.read.fetch_text", fetch_text)
    return [
        {"url": "https://down", "title": "D", "description": "the snippet"},
        {"url": "https://up", "title": "U", "description": "the snippet"},
    ]


def test_fetch_all_tells_which_pages_fell_back_to_the_snippet(monkeypatch):
    hits = snippet_hits(monkeypatch)

    assert list(fetch_all(hits)) == [("the snippet", True), ("the page", False)]


@pytest.mark.parametrize("from_snippet", [True, False])
def test_a_claim_knows_whether_it_was_read_from_a_snippet(from_snippet):
    model = DummyLM(
        [{"findings": [{"claim": "Found", "quote": "found in 1904", "confidence": "high"}]}]
    )
    hit = {"url": "https://a", "title": "A"}

    claims = intake(model, "b", "q", hit, "It was found in 1904.", 1, 1, None, from_snippet)

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


def test_unreadable_claims_cost_the_piece_not_the_run(monkeypatch):
    model = DummyLM([{"nonsense": "x"}] * 4)
    hit = {"url": "https://a", "title": "A"}

    assert intake(model, "brief", "question", hit, "text", 1, 1) == []


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

    sections, removed = verify.report(
        model, [("S", "A fact [1].")], {1: claim(1, "https://a")}, stream
    )

    assert sections == [("S", "A fact [1].")]
    assert removed == []


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
    claims = {1: claim(1, "https://a")}
    text = "> [!WARNING] Disputed\n> **Dates** differ [1].\n\n> [!NOTE]\n> Wrong [1].\n\nAfter [1]."
    asked = []

    def check(sentence, evidence):
        asked.append(sentence)
        return not sentence.startswith("Wrong"), "no"

    kept, removed = verify.verify("S", text, claims, check)

    assert kept == "> [!WARNING] Disputed\n> **Dates** differ [1].\n\n\nAfter [1]."
    assert [r.sentence for r in removed] == ["Wrong [1]."]
    # Bold is for the reader, not part of what is checked.
    assert asked[0] == "Dates differ [1]."


def test_the_style_pass_takes_out_filler_but_not_quotes_or_code():
    text = (
        "Notably, **Ugarit** fell 1190—1185 BC [1]. It is important to note that trade — "
        'the lifeline — was really very fragile. He wrote "very — late" and `a — b`.'
    )

    assert clean(text) == (
        "**Ugarit** fell 1190–1185 BC [1]. Trade, the lifeline, was fragile. "
        'He wrote "very — late" and `a — b`.'
    )
