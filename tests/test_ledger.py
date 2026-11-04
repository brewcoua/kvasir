"""The question ledger, with every model call, search and page faked."""

from __future__ import annotations

import numpy as np
import pytest
from dspy.utils.dummies import DummyLM

import kvasir.methods.ledger.loop as research_loop
from fakes import PAGE, Encoder, Logged, Search, Stream, claim, page, research_settings
from kvasir.methods.ledger.brief import clarifications
from kvasir.methods.ledger.compare import ordered, rank, verify_cells
from kvasir.methods.ledger.frontier import BRANCH_STALL, Frontier
from kvasir.methods.ledger.method import NO_EVIDENCE, run_research
from kvasir.methods.ledger.questions import Question, add_questions, open_questions
from kvasir.methods.ledger.repair import repair
from kvasir.methods.ledger.signatures import NamedQuestion, SearchFor
from kvasir.research.claims import Claim
from kvasir.research.corpus import Corpus, Piece
from kvasir.research.fetch import FetchError, extract
from kvasir.research.search import SearchUnavailable
from kvasir.research.verify import Verified
from kvasir.schemas.requests import Clarification, Input, LedgerOptions
from kvasir.schemas.results import (
    Cell,
    Criterion,
    RemovedSentence,
    RevisedSentence,
    Row,
    UnverifiedSentence,
)


def ask(question, **options):
    """The input and options of a ledger run."""
    return Input(question=question), LedgerOptions(**options)


@pytest.fixture
def settings():
    return research_settings()


def fake_models(monkeypatch, answers):
    models = {role: DummyLM(replies) for role, replies in answers.items()}
    monkeypatch.setattr("kvasir.methods.ledger.method.build_research_models", lambda *args: models)


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
            "decompose": [{"questions": [{"title": "t", "question": "When was the stone found?"}]}],
            # The question's search, then the repair's four for the removed sentence, which find
            # only pages already read.
            "plan": [{"query": "kvasir stone found"}] + [{"query": f"gold {n}"} for n in range(4)],
            # The same for both pages: they are read in parallel, so either may take either reply.
            # Neither quote is in the snippet page B falls back to.
            "intake": 2
            * [
                {
                    "findings": [
                        {
                            "claim": "Found in 1904",
                            "quote": "found in   1904",
                            "confidence": "high",
                        },
                        {"claim": "Made of gold", "quote": "solid gold", "confidence": "low"},
                    ]
                }
            ],
            "check": [{"status": "answered", "claims": [1, 7], "missing": "", "follow_ups": []}],
            "outline": [{"sections": ["Findings"]}],
            "write": [{"text": "It was found in 1904 [1][7]. It is made of gold [1]."}],
            # No summary, so the report is the sections alone.
            "tldr": [{"points": []}],
            "verify": [
                {"verdict": "supported", "reason": "the quote gives the year"},
                {"verdict": "unsupported", "reason": "the quote says nothing about gold"},
            ],
        },
    )
    hits = [
        {"url": "https://a", "title": "A", "description": "snippet a", "snippets": []},
        {"url": "https://b", "title": "B", "description": "snippet b", "snippets": []},
    ]
    monkeypatch.setattr(
        "kvasir.methods.ledger.method.build_research_tools",
        lambda settings: (Encoder(), Search(hits)),
    )

    def fake_fetch(url):
        if url == "https://b":
            raise FetchError("blocked")
        return page(extract(PAGE))

    monkeypatch.setattr("kvasir.research.read.fetch_page", fake_fetch)
    stream = Stream()

    result = run_research(settings, *ask(question="When was the Kvasir stone found?"), stream)

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
    # One event per intake call, each naming its page, the snippet fallback included.
    assert ("research", "search 1: read page 1/2: A") in stream.events
    assert ("research", "search 1: read page 2/2: B") in stream.events
    assert (
        "research",
        "search 1: 1 new claims, 1 in all; When was the stone found?: answered",
    ) in stream.events
    kinds = [(f.get("kind"), f.get("parent"), f.get("page")) for f in stream.fields]
    attempt = kinds.index(("attempt", None, None))
    assert stream.fields[attempt]["question"] == result.questions[0].id
    assert ("page", attempt, 1) in kinds and ("page", attempt, 2) in kinds
    assert ("result", attempt, None) in kinds
    assert kinds.count(("split", None, None)) == 1
    assert [f["kind"] for f in stream.fields if f.get("kind") == "section"] == ["section"]
    assert ("verify", "removed 1 of 2 cited sentences") in stream.events
    assert stream.events[-1] == ("verify", "found support for 0 of the sections they came from")


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
            "decompose": [{"questions": [{"title": "t", "question": "q?"}]}],
            "plan": [{"query": "q"}],
            "intake": [{"findings": []}],
        },
    )
    hits = [{"url": "https://a", "title": "A", "description": "nothing", "snippets": []}]
    monkeypatch.setattr(
        "kvasir.methods.ledger.method.build_research_tools",
        lambda settings: (Encoder(), Search(hits)),
    )
    monkeypatch.setattr("kvasir.research.read.fetch_page", lambda url: page("nothing relevant"))
    stream = Stream()

    result = run_research(settings, *ask(question="q", max_searches=1), stream)

    assert result.report == NO_EVIDENCE
    assert result.citations == []
    assert [(q.text, q.status) for q in result.questions] == [("q?", "open")]
    assert stream.events[-1] == ("write", "no evidence found, so nothing to write")


def test_clarifications_reach_the_brief_as_text():
    text = clarifications([Clarification(question="Region?", answer="Europe")])

    assert text == "Q: Region?\nA: Europe"
    assert clarifications([]) == "none"


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
            "decompose": [{"questions": [{"title": "t", "question": "How fast is each?"}]}],
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
            # Speed twice, the second pass reading the options in reverse. Beta's cost finding is
            # removed by verification, so no option has one and cost needs no judge.
            "judge": [{"ranking": ["Alpha", "Beta"]}, {"ranking": ["1. alpha"]}],
            "outline": [{"sections": ["Alpha"]}],
            "write": [{"text": "Alpha is the fastest [1]."}],
            # No summary, so the report is the sections alone.
            "tldr": [{"points": []}],
            # The three cells, then the sentence. Beta's cost is not in its quote.
            "verify": [
                {"verdict": "supported", "reason": "the quote gives the speed"},
                {"verdict": "supported", "reason": "the quote gives the speed"},
                {"verdict": "unsupported", "reason": "the quote says nothing of cost"},
                {"verdict": "supported", "reason": "the quote gives the speed"},
            ],
        },
    )
    hits = [{"url": "https://a", "title": "A", "description": "speeds", "snippets": []}]
    monkeypatch.setattr(
        "kvasir.methods.ledger.method.build_research_tools",
        lambda settings: (Encoder(), Search(hits)),
    )
    monkeypatch.setattr(
        "kvasir.research.read.fetch_page",
        lambda url: page("Alpha runs at 50 tok/s. Beta runs at 20 tok/s."),
    )
    stream = Stream()

    result = run_research(
        settings,
        *ask(question="Which is faster, Alpha or Beta?", output="ranking", max_attempts=1),
        stream,
    )

    assert result.output == "ranking"
    assert [(c.name, c.weight) for c in result.criteria] == [("speed", 3), ("cost", 1)]
    # Every cost ties at 1.5. Alpha: (3 x 1 + 1.5) / 4. Beta: (3 x 2 + 1.5) / 4.
    assert [(row.entity, row.rank) for row in result.matrix] == [("Alpha", 1), ("Beta", 2)]
    assert [(c.finding, c.claims, c.rank) for c in result.matrix[1].cells] == [
        ("20 tok/s", [2], 2.0),
        ("", [], 1.5),
    ]
    assert result.matrix[0].cells[1].rank == 1.5
    assert [(r.section, r.sentence, r.stage) for r in result.removed] == [
        ("Ranking", "Beta, cost: cheap [2]", "verify")
    ]
    # Both claims come from one page, so both cite source 1.
    assert result.report.startswith(
        "## Ranking\n\n| Rank | Option | speed | cost |\n| --- | --- | --- | --- |\n"
        "| 1 | Alpha | 50 tok/s [1] | – |\n"
        "| 2 | Beta | 20 tok/s [1] | – |\n"
    )
    assert result.report.endswith("## Alpha\n\nAlpha is the fastest [1].")
    # The page was read for the first question, so the search for the empty cell finds nothing.
    assert ("assess", "filled 0 of 1 empty cells") in stream.events
    assert ("assess", "ranked: Alpha, Beta") in stream.events


class Checker:
    """A grounding checker answering from a list, and logging each call."""

    def __init__(self, answers, log):
        self.answers, self.log = list(answers), log

    def __call__(self, prompt):
        self.log.append("checker")
        return [self.answers.pop(0)]


def check_cells(checker, fast=(), reason=()):
    """Two options with one cell each, `verify_cells` run over them, and what it left behind."""
    log = []
    models = {
        "ground": Checker(checker, log),
        "verify": Logged("fast", log, fast),
        "escalate": Logged("escalate", log, reason),
    }
    rows = [
        Row(entity=name, cells=[Cell(criterion="speed", finding="50 tok/s", claims=[1])])
        for name in ("Alpha", "Beta")
    ]
    stream = Stream()
    by_id = {1: claim(1, "https://a")}
    removed, unverified = verify_cells(models, rows, by_id, stream, "Ranking")
    return rows, removed, unverified, log, stream


def test_a_cell_the_checker_supports_is_unchanged():
    rows, removed, unverified, log, _ = check_cells(["Yes", "Yes"])

    assert [row.cells[0].finding for row in rows] == ["50 tok/s", "50 tok/s"]
    assert removed == unverified == []
    assert log == ["checker", "checker"]


def test_a_cell_the_cascade_removes_is_emptied_and_ranks_last():
    rows, removed, _, _, _ = check_cells(
        ["Yes", "No"], fast=[{"verdict": "unsupported", "reason": "no speed"}]
    )
    ranked = rank(DummyLM([]), "brief", [Criterion(name="speed")], rows, Stream())

    assert (rows[1].cells[0].finding, rows[1].cells[0].claims) == ("", [])
    assert [(r.section, r.sentence, r.stage) for r in removed] == [
        ("Ranking", "Beta, speed: 50 tok/s [1]", "verify")
    ]
    assert [row.entity for row in ranked] == ["Alpha", "Beta"]
    assert ranked[1].cells[0].rank == 2


def test_a_partly_supported_cell_is_emptied_not_rewritten():
    rows, removed, _, _, _ = check_cells(
        ["No", "Yes", "Yes"],
        fast=[{"verdict": "partial", "reason": "r"}],
        reason=[{"verdict": "partial", "reason": "the speed is rounded", "rewrite": "50 [1]"}],
    )

    assert rows[0].cells[0].finding == ""
    assert rows[1].cells[0].finding == "50 tok/s"
    assert [(r.stage, r.reason) for r in removed] == [
        (
            "rewrite",
            "a partly supported cell is removed rather than rewritten (the speed is rounded)",
        )
    ]


def test_a_cell_no_step_can_judge_keeps_its_finding_marked():
    rows, _, unverified, _, _ = check_cells(
        ["No", "Yes"], fast=[{"verdict": "partial", "reason": "r"}], reason=[{"verdict": "x"}]
    )

    assert rows[0].cells[0].finding == "50 tok/s [unverified]"
    assert [(u.section, u.sentence) for u in unverified] == [
        ("Ranking", "Alpha, speed: 50 tok/s [1]")
    ]


def test_every_cell_is_checked_before_the_fast_model_and_stays_on_the_assess_stage():
    _, _, _, log, stream = check_cells(
        ["No", "No"], fast=[{"verdict": "unsupported", "reason": "r"}] * 2
    )

    assert log == ["checker", "checker", "fast", "fast"]
    assert {stage for stage, _ in stream.events} == {"assess"}


def test_a_ranked_name_matches_without_its_number_or_case():
    assert ordered(["2) beta", "Gamma", "BETA", "1. Alpha"], ["Alpha", "Beta", "Delta"]) == [
        "Beta",
        "Alpha",
        "Delta",
    ]


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
            "decompose": [{"questions": [{"title": "t", "question": "q?"}]}],
            "plan": [{"query": "q"}],
            "intake": [{"findings": []}],
        },
    )
    hits = [{"url": "https://a", "title": "A", "description": "nothing", "snippets": []}]
    monkeypatch.setattr(
        "kvasir.methods.ledger.method.build_research_tools",
        lambda settings: (Encoder(), Search(hits)),
    )
    monkeypatch.setattr("kvasir.research.read.fetch_page", lambda url: page("nothing relevant"))
    stream = Stream()

    run_research(
        settings,
        *ask(question="Rank the Pixel and the iPhone on price and privacy", max_searches=1),
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
            "decompose": [{"questions": [{"title": "t", "question": "q?"}]}],
            "entities": [{"entities": ["Pixel 10", "Phantom 9"]}],
            "intake": [{"findings": []}],
        },
    )
    hits = [{"url": "https://a", "title": "A", "description": "The Pixel 10 leads", "snippets": []}]
    monkeypatch.setattr(
        "kvasir.methods.ledger.method.build_research_tools",
        lambda settings: (Encoder(), Search(hits)),
    )
    monkeypatch.setattr("kvasir.research.read.fetch_page", lambda url: page("nothing relevant"))
    stream = Stream()

    run_research(settings, *ask(question="Rank phones on price", max_searches=1), stream)

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
            "decompose": [{"questions": [{"title": "t", "question": "q?"}]}],
            "plan": [{"query": "q1"}, {"query": "q2"}, {"query": "q3"}],
            "intake": [],
        },
    )
    failing = Search([], ["brave: Suspended: too many requests", "duckduckgo: CAPTCHA"])
    monkeypatch.setattr(
        "kvasir.methods.ledger.method.build_research_tools",
        lambda settings: (Encoder(), failing),
    )

    with pytest.raises(SearchUnavailable) as raised:
        run_research(settings, *ask(question="q"), Stream())

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


def loop(
    monkeypatch,
    questions,
    found,
    judge,
    budget=16,
    max_attempts=4,
    search=None,
    frontier=None,
    recalled=None,
    stream=None,
    **options,
):
    """Run `_research` over `questions`, the model writing `query N` and reading `found`.

    `found` maps a query number to the claim texts its page yields. `recalled` maps the number of
    searches made so far to the claim texts a recall then yields; each search adds a piece to the
    corpus, as reading its page does. Returns the search, the shapes
    the model was asked for and the claims gathered.
    """
    shapes = []

    def predict(model, signature, **inputs):
        assert signature is SearchFor
        shapes.append(inputs["shape"])
        return type("Prediction", (), {"query": f"query {len(shapes)}"})

    def read(
        model,
        encoder,
        brief,
        question,
        hits,
        first_id,
        step,
        label,
        stream,
        corpus,
        checker,
        attempt=None,
        stage="research",
    ):
        texts = found.get(len(shapes), []) if hits else []
        corpus.pieces.append(Piece(page("p"), "piece", np.zeros(1)))
        return [claim(first_id + i, hits[0]["url"]) for i, _ in enumerate(texts)]

    monkeypatch.setattr(research_loop, "predict", predict)

    def recall(
        model,
        encoder,
        brief,
        question,
        corpus,
        first_id,
        step,
        label,
        stream,
        checker,
        **fields,
    ):
        texts = (recalled or {}).get(len(shapes), [])
        return [claim(first_id + i, "https://held") for i, _ in enumerate(texts)]

    monkeypatch.setattr(research_loop, "read", read)
    monkeypatch.setattr(research_loop, "recall", recall)
    search = search or Pages()
    corpus = Corpus()
    claims: list[Claim] = []
    searches = research_loop.research(
        {"plan": None, "intake": None},
        Encoder(),
        search,
        "2026-11-01",
        "brief",
        questions,
        claims,
        set(),
        corpus,
        budget,
        1,
        max_attempts,
        judge,
        0,
        stream or Stream(),
        frontier(claims) if frontier else None,
        **options,
    )
    return searches, shapes, claims


def test_a_question_nothing_answers_is_tried_in_every_shape_then_given_up(monkeypatch):
    question = Question(id=1, text="When?")

    searches, shapes, _ = loop(monkeypatch, [question], {}, judge=lambda q: None)

    assert searches == 4
    assert shapes == list(research_loop.QUERY_SHAPES)
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


def test_a_question_recall_answers_costs_no_search(monkeypatch):
    first, second = Question(id=1, text="a"), Question(id=2, text="b")
    search = Pages()

    def judge(question):
        question.judged_at = 1
        question.status = "answered"

    # The first search leaves a piece that answers the second question.
    loop(monkeypatch, [first, second], {1: ["a"]}, judge, search=search, recalled={1: ["b"]})

    assert len(search.queries) == 1
    assert second.queries == []
    assert second.status == "answered"


def test_a_recall_that_finds_nothing_leads_to_a_search(monkeypatch):
    question = Question(id=1, text="a")
    search = Pages()

    loop(monkeypatch, [question], {}, judge=lambda q: None, max_attempts=1, search=search)

    assert question.queries == ["query 1"]
    assert len(search.queries) == 1


def settle(question):
    question.judged_at = 99


def test_a_question_recall_leaves_open_is_still_searched(monkeypatch):
    question = Question(id=1, text="a")
    search = Pages()

    loop(
        monkeypatch,
        [question],
        {},
        judge=settle,
        max_attempts=1,
        search=search,
        recalled={0: ["half"]},
    )

    assert len(search.queries) == 1


def test_recall_does_not_consume_the_budget(monkeypatch):
    questions = [Question(id=1, text="a"), Question(id=2, text="b")]
    search = Pages()

    def judge(question):
        question.judged_at = 99

    searches, _, claims = loop(
        monkeypatch,
        questions,
        {},
        judge=judge,
        budget=3,
        search=search,
        recalled={0: ["x"], 1: ["y"], 2: ["z"]},
    )

    assert searches == 3
    assert len(search.queries) == 3
    assert len(claims) > 0


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
    assert shapes == [research_loop.QUERY_SHAPES[0]] * 3
    assert question.queries == ["query 3"]


def test_the_judge_counts_only_claims_it_was_shown(monkeypatch):
    claims = [claim(1, "https://a"), claim(2, "https://b")]
    question = Question(id=1, text="When?")
    model = DummyLM(
        [{"status": "answered", "claims": [2, 9], "missing": "", "follow_ups": ["Why?"]}]
    )

    asked = research_loop.check(model, Encoder(), "brief", claims, question)

    assert (question.status, question.claims, question.judged_at) == ("answered", [2], 2)
    assert asked == ["Why?"]
    # Nothing new among the closest claims: no call, which would exhaust the fake.
    assert research_loop.check(model, Encoder(), "brief", claims, question) == []


def test_an_answer_resting_on_no_shown_claim_is_open(monkeypatch):
    question = Question(id=1, text="When?")
    model = DummyLM(
        [{"status": "answered", "claims": [9], "missing": "the year", "follow_ups": ["Why?"]}]
    )

    asked = research_loop.check(model, Encoder(), "brief", [claim(1, "https://a")], question)

    assert (question.status, question.claims, question.missing) == ("open", [], "the year")
    assert asked == []


def test_a_repeated_question_is_not_added(monkeypatch):
    class Words:
        def encode(self, texts, query=False):
            return [[1.0, 0.0] if "year" in text else [0.0, 1.0] for text in texts]

    ledger = [Question(id=1, text="Which year?")]
    parent = ledger[0]

    asked = ["The year it was found?", "Who found it?", " "]
    add_questions(Words(), ledger, [NamedQuestion(title="t", question=q) for q in asked], parent)

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

    assert open_questions(ledger) == (
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

    def research(questions, budget, judge, stage, parent):
        searched.append([(q.text, q.missing) for q in questions])
        claims.append(claim(2, "https://b", quote="solid gold"))
        questions[0].status, questions[0].claims = "answered", [2]

    models = {
        "write": DummyLM([{"text": "It is gold [2]. It shines [2]."}]),
        "verify": DummyLM(
            [
                {"verdict": "supported", "reason": "says gold"},
                {"verdict": "unsupported", "reason": "no shine"},
            ]
        ),
    }

    checked = repair(
        models,
        Encoder(),
        "brief",
        ["Findings"],
        claims,
        5,
        Verified(
            [("Ranking", "| table |"), ("Findings", "Found in 1904 [1].")],
            removed,
            # Stale once the section is written again.
            [RevisedSentence(section="Findings", before="a [1].", after="b [1].", reason="r")],
            [UnverifiedSentence(section="Findings", sentence="c [1].", reason="r")],
        ),
        research,
        lambda q: None,
        Stream(),
    )

    # Only sentences from written sections: the ranking table is built, not written.
    assert searched == [[("It is gold.", "no gold")]]
    assert checked.sections == [("Ranking", "| table |"), ("Findings", "It is gold [2].")]
    assert checked.revised == checked.unverified == []
    assert [(r.section, r.sentence) for r in checked.removed] == [
        ("Ranking", "Row [1]."),
        ("Findings", "It shines [2]."),
    ]


def test_the_frontier_stops_searching_a_branch_that_adds_nothing(monkeypatch):
    spent, answering = Question(id=1, text="a"), Question(id=2, text="b")
    found = {2: ["x"], 4: ["y"], 5: ["z"], 6: ["w"]}

    held = []

    # Judged on every claim so far, so a question is not judged again until more arrive.
    def judge(question):
        question.judged_at = len(held[0])

    def frontier(claims):
        held.append(claims)
        return Frontier(Encoder(), "brief", claims)

    searches, _, _ = loop(monkeypatch, [spent, answering], found, judge, frontier=frontier)

    # Breadth first, the first question would get every query shape before the run gave up on it.
    assert spent.attempts == BRANCH_STALL
    assert answering.attempts == 4
    assert searches == BRANCH_STALL + 4


def test_a_site_query_stands_in_for_the_planned_query_of_a_later_attempt(monkeypatch):
    question = Question(id=1, text="a")
    asked = []

    def site_query(self, question_id, text, claims):
        asked.append(question_id)
        return "site:example.org a"

    monkeypatch.setattr(Corpus, "site_query", site_query)

    _, shapes, _ = loop(monkeypatch, [question], {}, judge=lambda q: None, max_attempts=2)

    # The first attempt is planned; the second needs no planner call.
    assert question.queries == ["query 1", "site:example.org a"]
    assert len(shapes) == 1
    assert asked == [1]


def test_a_ledger_run_records_its_questions_before_the_research_ends(settings, monkeypatch):
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
            "decompose": [{"questions": [{"title": "t", "question": "q?"}]}],
            "plan": [{"query": "q"}],
            "intake": [{"findings": []}],
        },
    )
    hits = [{"url": "https://a", "title": "A", "description": "nothing", "snippets": []}]
    monkeypatch.setattr(
        "kvasir.methods.ledger.method.build_research_tools",
        lambda settings: (Encoder(), Search(hits)),
    )
    monkeypatch.setattr("kvasir.research.read.fetch_page", lambda url: page("nothing relevant"))
    stream = Stream()
    seen = []

    def record(questions):
        seen.append((len(stream.events), [(q.id, q.status) for q in questions]))

    monkeypatch.setattr("kvasir.methods.ledger.method.record_questions", record)
    monkeypatch.setattr(research_loop, "record_questions", record)

    run_research(settings, *ask(question="q", max_searches=1), stream)

    result = next(i for i, f in enumerate(stream.fields) if f.get("kind") == "result")
    # Once the questions exist, then before the result event, with the status it reports.
    assert seen[0] == (2, [(1, "open")])
    assert seen[-1] == (result, [(1, "open")])
    assert stream.events[result][1].endswith("q?: open")


def test_a_repair_searches_under_its_own_event_and_leaves_the_ledger_alone(monkeypatch):
    recorded = []
    monkeypatch.setattr(research_loop, "record_questions", recorded.append)
    stream = Stream()
    removed = [
        RemovedSentence(section="Findings", sentence="One [1].", reason="no"),
        RemovedSentence(section="Findings", sentence="Two [1].", reason="no"),
    ]

    def research(questions, budget, judge, stage, parent):
        loop(
            monkeypatch,
            questions,
            {},
            judge,
            budget=2,
            stream=stream,
            stage=stage,
            parent=parent,
        )

    repair(
        {},
        Encoder(),
        "brief",
        ["Findings"],
        [claim(1, "https://a")],
        3,
        Verified([("Findings", "One [1].")], removed, [], []),
        research,
        lambda q: None,
        stream,
    )

    marker = next(i for i, f in enumerate(stream.fields) if f.get("kind") == "repair")
    searching = stream.fields[marker + 1 :]
    assert {stage for stage, _ in stream.events} == {"verify"}
    assert [f["parent"] for f in searching if f.get("kind") == "attempt"] == [marker, marker]
    assert sorted({f["question"] for f in searching if "question" in f}) == [3, 4]
    assert recorded == []
