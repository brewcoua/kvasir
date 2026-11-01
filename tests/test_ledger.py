"""The question ledger, with every model call, search and page faked."""

from __future__ import annotations

import pytest
from dspy.utils.dummies import DummyLM

import kvasir.methods.ledger.loop as research_loop
from fakes import PAGE, Encoder, Search, Stream, claim, research_settings
from kvasir.methods.ledger.brief import clarifications
from kvasir.methods.ledger.compare import ordered
from kvasir.methods.ledger.frontier import BRANCH_STALL, Frontier
from kvasir.methods.ledger.method import NO_EVIDENCE, run_research
from kvasir.methods.ledger.questions import Question, add_questions, open_questions
from kvasir.methods.ledger.repair import repair
from kvasir.methods.ledger.signatures import NamedQuestion, SearchFor
from kvasir.research.claims import Claim
from kvasir.research.fetch import FetchError, extract
from kvasir.research.search import SearchUnavailable
from kvasir.schemas.requests import Clarification, Input, LedgerOptions
from kvasir.schemas.results import RemovedSentence


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
        "kvasir.methods.ledger.method.build_research_tools",
        lambda settings: (Encoder(), Search(hits)),
    )

    def fake_fetch(url):
        if url == "https://b":
            raise FetchError("blocked")
        return extract(PAGE)

    monkeypatch.setattr("kvasir.research.read.fetch_text", fake_fetch)
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
    # One event per piece read, each naming its page, the snippet fallback included.
    assert ("research", "search 1: page 1/2, part 1/1: A") in stream.events
    assert ("research", "search 1: page 2/2, part 1/1: B") in stream.events
    assert (
        "research",
        "search 1: 1 new claims, 1 in all; When was the stone found?: answered",
    ) in stream.events
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
    monkeypatch.setattr("kvasir.research.read.fetch_text", lambda url: "nothing relevant")
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
        "kvasir.methods.ledger.method.build_research_tools",
        lambda settings: (Encoder(), Search(hits)),
    )
    monkeypatch.setattr(
        "kvasir.research.read.fetch_text",
        lambda url: "Alpha runs at 50 tok/s. Beta runs at 20 tok/s.",
    )
    stream = Stream()

    result = run_research(
        settings,
        *ask(question="Which is faster, Alpha or Beta?", output="ranking", max_attempts=1),
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
    monkeypatch.setattr("kvasir.research.read.fetch_text", lambda url: "nothing relevant")
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
    monkeypatch.setattr("kvasir.research.read.fetch_text", lambda url: "nothing relevant")
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
    monkeypatch, questions, found, judge, budget=16, max_attempts=4, search=None, frontier=None
):
    """Run `_research` over `questions`, the model writing `query N` and reading `found`.

    `found` maps a query number to the claim texts its page yields. Returns the search, the shapes
    the model was asked for and the claims gathered.
    """
    shapes = []

    def predict(model, signature, **inputs):
        assert signature is SearchFor
        shapes.append(inputs["shape"])
        return type("Prediction", (), {"query": f"query {len(shapes)}"})

    def read(model, encoder, brief, question, hits, first_id, step, label, stream, checker):
        texts = found.get(len(shapes), []) if hits else []
        return [claim(first_id + i, hits[0]["url"]) for i, _ in enumerate(texts)]

    monkeypatch.setattr(research_loop, "predict", predict)
    monkeypatch.setattr(research_loop, "read", read)
    search = search or Pages()
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
        budget,
        1,
        max_attempts,
        judge,
        0,
        Stream(),
        frontier(claims) if frontier else None,
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
        def encode(self, texts):
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

    sections, left = repair(
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
