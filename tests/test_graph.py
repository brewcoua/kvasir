"""The graph method, with every model call, search and page faked."""

from __future__ import annotations

import math
import zlib

import numpy as np
from dspy.utils.dummies import DummyLM

import kvasir.methods.graph.explore as graph_explore
import kvasir.methods.graph.method as graph_method
from fakes import PAGE, Search, Stream, Vectors, claim, research_settings
from kvasir.methods.graph.explore import explore
from kvasir.methods.graph.graph import branches, path
from kvasir.methods.graph.method import outline, run_graph
from kvasir.methods.graph.proof import pick, work
from kvasir.methods.graph.signatures import Expand, Gaps, Section
from kvasir.methods.ledger.frontier import Frontier
from kvasir.methods.ledger.questions import Question
from kvasir.methods.ledger.signatures import NamedQuestion, SearchFor
from kvasir.research.claims import Claim
from kvasir.research.fetch import extract
from kvasir.schemas.requests import GraphOptions, Input


class Scattered:
    """An encoder giving each text its own random vector, so no two texts are close."""

    def encode(self, texts, query=False):
        return np.array(
            [np.random.default_rng(zlib.crc32(t.encode())).normal(size=64) for t in texts]
        )


def test_a_path_runs_from_the_first_question_and_a_branch_holds_its_descendants():
    first = Question(id=1, text="a")
    child = Question(id=2, text="b", parent=1, depth=1)
    grandchild = Question(id=3, text="c", parent=2, depth=2)
    other = Question(id=4, text="d")
    questions = [first, child, grandchild, other]

    assert path(questions, grandchild) == [first, child]
    assert path(questions, first) == []
    assert branches(questions) == {1: [first, child, grandchild], 4: [other]}


def explored(
    monkeypatch,
    questions,
    budget=10,
    max_depth=2,
    max_attempts=3,
    deadline=10**9,
    status=lambda question, path: "answered",
    raised=lambda question: [question + " more"],
    found=lambda question: 1,
    gaps=lambda: [],
):
    """Explore `questions`. A search for a question finds `found(question)` new claims, and its
    judgement gives it `status(question, path)` and raises `raised(question)`. Returns the
    searches, the (path, question) of each judgement, and the stream."""
    judged = []

    def predict(model, signature, **inputs):
        if signature is SearchFor:
            return type("Prediction", (), {"query": inputs["question"]})
        if signature is Gaps:
            new = [NamedQuestion(title="gap", question=q) for q in gaps()]
            return type("Prediction", (), {"raised": new})
        assert signature is Expand
        judged.append((inputs["path"], inputs["question"]))
        ids = [int(line[1:].split("]")[0]) for line in inputs["evidence"].splitlines()[::2]]
        return type(
            "Prediction",
            (),
            {
                "status": status(inputs["question"], inputs["path"]),
                "claims": ids,
                "missing": "",
                "raised": [
                    NamedQuestion(title="more", question=q) for q in raised(inputs["question"])
                ],
            },
        )

    def read(model, encoder, brief, question, hits, first_id, step, label, stream, checker):
        return [
            claim(i, f"https://site{i}.org") for i in range(first_id, first_id + found(question))
        ]

    class Pages:
        failures: list[str] = []

        def __call__(self, query, exclude_urls):
            return [{"url": f"https://{query}", "title": query, "description": "", "snippets": []}]

    monkeypatch.setattr(graph_explore, "predict", predict)
    monkeypatch.setattr(graph_explore, "read", read)
    claims: list[Claim] = []
    stream = Stream()
    encoder = Scattered()
    searches = explore(
        {"plan": None, "intake": None, "check": None, "decompose": None},
        encoder,
        Pages(),
        "2026-11-01",
        "brief",
        questions,
        claims,
        Frontier(encoder, "brief", claims),
        deadline,
        budget,
        1,
        max_attempts,
        max_depth,
        stream,
    )
    return searches, judged, stream


def test_a_judgement_reads_its_path_and_raises_children_down_to_the_depth_cap(monkeypatch):
    questions = [Question(id=1, text="a")]

    searches, judged, _ = explored(
        monkeypatch, questions, budget=2, max_depth=1, status=lambda q, p: "weak"
    )

    # The child is judged under its parent, and at the depth cap raises nothing.
    assert searches == 2
    assert judged == [("none", "a"), ("a", "a more")]
    assert [(q.text, q.parent, q.depth, q.status) for q in questions] == [
        ("a", None, 0, "weak"),
        ("a more", 1, 1, "weak"),
    ]


def test_an_answered_question_raises_one_harder_follow_up(monkeypatch):
    questions = [Question(id=1, text="a")]

    searches, _, _ = explored(monkeypatch, questions, raised=lambda q: [f"{q} {i}" for i in "xyz"])

    # Each answered, down to the depth cap.
    assert searches == 3
    assert [(q.text, q.parent) for q in questions] == [("a", None), ("a x", 1), ("a x x", 2)]


def test_a_question_earns_children_as_it_is_searched_again(monkeypatch):
    questions = [Question(id=1, text="a")]

    explored(
        monkeypatch,
        questions,
        budget=1,
        status=lambda q, p: "weak",
        raised=lambda q: [f"{q} {i}" for i in range(5)],
    )

    # Searched once, it holds ceil(2 * 1 ** 0.5) children.
    assert [q.text for q in questions] == ["a", "a 0", "a 1"]


def test_the_research_stops_at_its_deadline(monkeypatch):
    clock = iter([0, 0, 10, 10])
    monkeypatch.setattr(graph_explore.time, "monotonic", lambda: next(clock))
    questions = [Question(id=1, text="a"), Question(id=2, text="b")]

    searches, _, _ = explored(monkeypatch, questions, deadline=5)

    assert searches == 1


def test_a_parent_is_judged_again_once_its_children_are_answered(monkeypatch):
    questions = [Question(id=1, text="a")]
    calls = {"a": 0}

    def status(question, path):
        if question != "a":
            return "answered"
        calls["a"] += 1
        return "weak" if calls["a"] == 1 else "answered"

    searches, judged, _ = explored(monkeypatch, questions, max_depth=1, status=status)

    # Its child's claims answer `a` without another search for it.
    assert searches == 2
    assert judged[-1] == ("none", "a")
    assert questions[0].status == "answered"
    assert questions[0].attempts == 1


def test_a_branch_closes_once_two_searches_in_a_row_add_nothing(monkeypatch):
    questions = [Question(id=1, text="a"), Question(id=2, text="b")]

    _, _, stream = explored(
        monkeypatch,
        questions,
        status=lambda q, p: "weak",
        raised=lambda q: [],
        found=lambda q: 4 if q == "a" else 0,
    )

    assert questions[0].attempts == 3
    assert questions[1].attempts == 2
    assert ("research", "closing the branch of b: 2 searches in a row added nothing new") in (
        stream.events
    )


def test_work_is_the_cheaper_of_a_retry_and_settling_every_child():
    retried = Question(id=1, text="a", status="weak", queries=["q"])
    split = Question(id=2, text="b", status="weak", queries=["q", "q"])
    answered = Question(id=3, text="c", parent=2, depth=1, status="answered", queries=["q"])
    untried = Question(id=4, text="d", parent=2, depth=1)
    spent = Question(id=5, text="e", status="unresolved", queries=["q", "q", "q"])
    closed = Question(id=6, text="f")
    questions = [retried, split, answered, untried, spent, closed]

    assert work(questions, 3, {6}) == {1: 2, 2: 1, 3: 0, 4: 1, 5: math.inf, 6: math.inf}


def test_the_pick_walks_down_to_a_child_while_the_children_cost_no_more_than_a_retry():
    first = Question(id=1, text="a", status="weak", queries=["q"])
    children = [Question(id=i, text=f"c{i}", parent=1, depth=1) for i in (2, 3)]
    frontier = Frontier(Scattered(), "brief", [])

    # A retry costs 2, the two untried children 2 together: the children go first.
    assert pick([first, *children], frontier, 3) in children
    # With a third, they cost more than the retry.
    third = Question(id=4, text="c4", parent=1, depth=1)
    assert pick([first, *children, third], frontier, 3) is first
    frontier.closed.add(1)
    assert pick([first, *children, third], frontier, 3) is None


def test_a_run_writes_one_section_per_branch_that_found_something(monkeypatch):
    models = {
        role: DummyLM(replies)
        for role, replies in {
            "brief": [
                {
                    "brief": "b",
                    "assumptions": [],
                    "output": "report",
                    "criteria": [],
                    "entities": [],
                }
            ],
            "decompose": [
                {
                    "questions": [
                        {"title": "first", "question": "Q1?"},
                        {"title": "second", "question": "Q2?"},
                    ]
                }
            ],
            "plan": [{"query": "one"}, {"query": "two"}],
            "intake": [
                {
                    "findings": [
                        {"claim": "Found in 1904", "quote": "found in 1904", "confidence": "high"}
                    ]
                }
            ],
            "check": [
                {
                    "status": "answered",
                    "claims": [1],
                    "missing": "",
                    "raised": [{"title": "third", "question": "Q3?"}],
                }
            ],
            "outline": [{"sections": [{"title": "The find", "questions": [1]}]}],
            "write": [{"text": "It was found in 1904 [1]."}],
            "tldr": [{"points": ["It was found in **1904** [1]."]}],
            "verify": 2 * [{"supported": True, "reason": "the quote gives the year"}],
        }.items()
    }
    monkeypatch.setattr("kvasir.methods.graph.method.build_research_models", lambda *a: models)
    encoder = Vectors(
        {
            "b": [1, 0, 0],
            "Q1?": [0, 1, 0],
            "Q2?": [0, 0, 1],
            "Q3?": [0, 1, 1],
            "Found in 1904": [0, 1, 0],
        },
        default=[1, 1, 1],
    )
    hits = [{"url": "https://a", "title": "A", "description": "", "snippets": []}]
    monkeypatch.setattr(
        "kvasir.methods.graph.method.build_research_tools", lambda s: (encoder, Search(hits))
    )
    monkeypatch.setattr("kvasir.research.read.fetch_text", lambda url: extract(PAGE))

    result = run_graph(
        research_settings(),
        Input(question="When was the stone found?"),
        GraphOptions(max_searches=2, max_depth=1),
        Stream(),
    )

    # Q2's search finds only the page already read; Q3 is raised under Q1 but never reached.
    assert result.report == (
        "> [!SUMMARY] TL;DR\n> - It was found in **1904** [1].\n\n"
        "## The find\n\nIt was found in 1904 [1].\n\n## Open questions\n\n- Q2? (not found)"
    )
    assert [(q.title, q.text, q.parent, q.status) for q in result.questions] == [
        ("first", "Q1?", None, "answered"),
        ("second", "Q2?", None, "open"),
        ("third", "Q3?", 1, "open"),
    ]
    assert [c.url for c in result.citations] == ["https://a"]


def test_the_follow_ups_of_an_answered_question_are_still_reached():
    answered = Question(id=1, text="a", status="answered", queries=["q"])
    follow_up = Question(id=2, text="b", parent=1, depth=1)
    frontier = Frontier(Scattered(), "brief", [])

    assert work([answered, follow_up], 3, set()) == {1: 1, 2: 1}
    assert pick([answered, follow_up], frontier, 3) is follow_up


def test_children_all_answered_leave_an_unanswered_question_to_a_retry():
    weak = Question(id=1, text="a", status="weak", queries=["q"])
    answered = Question(id=2, text="b", parent=1, depth=1, status="answered", queries=["q"])
    follow_up = Question(id=3, text="c", parent=2, depth=2)
    frontier = Frontier(Scattered(), "brief", [])

    assert work([weak, answered, follow_up], 3, set())[1] == 2
    assert pick([weak, answered, follow_up], frontier, 3) is weak
    # Given up, what is left below it is its children's follow-ups.
    weak.status = "unresolved"
    assert pick([weak, answered, follow_up], frontier, 3) is follow_up


def outlined(monkeypatch, questions, sections, vectors=None):
    shown = []

    def predict(model, signature, **inputs):
        shown.append(inputs["questions"])
        return type("Prediction", (), {"sections": sections})

    monkeypatch.setattr(graph_method, "predict", predict)
    return outline(None, Vectors(vectors or {}, default=[1, 0]), "brief", questions), shown


def test_the_outline_merges_a_repeated_title_and_files_a_left_out_question_closest(monkeypatch):
    questions = [
        Question(id=1, text="Q1?", title="first", claims=[1]),
        Question(id=2, text="Q2?", title="second", claims=[2]),
        Question(id=3, text="Q3?", parent=1, depth=1, claims=[3]),
        Question(id=4, text="Q4?", title="fourth", claims=[4]),
        Question(id=5, text="Q5?"),
    ]
    sections = [
        Section(title="Causes", questions=[1], subsections=["Drought"]),
        Section(title="Effects", questions=[3, 2]),
        Section(title="causes", questions=[2], subsections=["Drought", "Trade"]),
        Section(title="Nothing", questions=[5]),
    ]
    vectors = {"Causes": [1, 0], "Effects": [0, 1], "Q4?": [0.1, 1]}

    found, shown = outlined(monkeypatch, questions, sections, vectors)

    assert found == [("Causes", ["Drought", "Trade"], {1, 2}), ("Effects", [], {2, 3, 4})]
    # Only questions with claims are shown.
    assert "[5]" not in shown[0]


def test_the_outline_keeps_seven_sections_at_most(monkeypatch):
    questions = [Question(id=n, text=f"Q{n}?", claims=[n]) for n in range(1, 10)]
    sections = [Section(title=f"S{n}", questions=[n]) for n in range(1, 10)]

    found, _ = outlined(monkeypatch, questions, sections)

    assert [title for title, _, _ in found] == [f"S{n}" for n in range(1, 8)]
    assert set().union(*(ids for _, _, ids in found)) == set(range(1, 10))


def test_an_outline_with_no_usable_section_gives_each_branch_one(monkeypatch):
    questions = [
        Question(id=1, text="Q1?", title="first", claims=[1]),
        Question(id=2, text="Q2?", parent=1, depth=1, claims=[2]),
        Question(id=3, text="Q3?", claims=[3]),
    ]

    found, _ = outlined(monkeypatch, questions, [Section(title="Empty", questions=[9])])

    assert found == [("first", [], {1, 2}), ("Q3?", [], {3})]


def test_once_no_question_is_left_a_gap_call_adds_first_questions_until_two_add_none(monkeypatch):
    questions = [Question(id=1, text="a")]
    asked = iter([["b", "c"], [], ["d"], [], []])

    searches, _, stream = explored(
        monkeypatch,
        questions,
        max_depth=1,
        raised=lambda q: [],
        found=lambda q: 2,
        gaps=lambda: next(asked),
    )

    assert [(q.text, q.parent) for q in questions] == [
        ("a", None),
        ("b", None),
        ("c", None),
        ("d", None),
    ]
    assert searches == 4
    gap_events = [d for _, d in stream.events if d.startswith("no question left")]
    assert len(gap_events) == 5


def test_the_gap_calls_stop_once_the_questions_two_added_found_little(monkeypatch):
    questions = [Question(id=1, text="a")]
    asked = iter([["b", "c"], ["d"], ["e"]])

    searches, _, stream = explored(
        monkeypatch, questions, max_depth=1, raised=lambda q: [], gaps=lambda: next(asked)
    )

    # One claim a search: each gap call's questions count as dry, and two in a row end it.
    assert [q.text for q in questions] == ["a", "b", "c", "d"]
    assert searches == 4
    assert stream.events[-1][1] == "stopping: the last 2 gaps found little or nothing new"


def test_open_questions_are_the_first_questions_left_then_raised_ones_answered_in_part():
    questions = [
        Question(id=1, text="Q1?", status="open", queries=["a", "b"]),
        Question(id=2, text="Q2?", parent=1, depth=1, status="weak", queries=["c"]),
        Question(id=3, text="Q3?", parent=1, depth=1, status="unresolved", queries=["d"]),
        Question(id=4, text="Q4?", status="answered", queries=["e"]),
        Question(id=5, text="Q5?"),
    ]

    assert graph_method.open_questions(questions) == (
        "- Q1? (not found)\n- Q5? (not searched)\n- Q2? (partly answered)"
    )
