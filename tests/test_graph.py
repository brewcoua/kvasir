"""The graph method, with every model call, search and page faked."""

from __future__ import annotations

import zlib

import numpy as np
from dspy.utils.dummies import DummyLM

import kvasir.methods.graph.explore as graph_explore
from fakes import PAGE, Search, Stream, Vectors, claim, research_settings
from kvasir.methods.graph.explore import explore
from kvasir.methods.graph.graph import branches, path
from kvasir.methods.graph.method import run_graph
from kvasir.methods.graph.signatures import Expand
from kvasir.methods.ledger.frontier import Frontier
from kvasir.methods.ledger.questions import Question
from kvasir.methods.ledger.signatures import NamedQuestion, SearchFor
from kvasir.research.claims import Claim
from kvasir.research.fetch import extract
from kvasir.schemas.requests import GraphOptions, Input


class Scattered:
    """An encoder giving each text its own random vector, so no two texts are close."""

    def encode(self, texts):
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


def explored(monkeypatch, questions, budget=10, max_depth=2, branch_searches=6):
    """Explore `questions`, each search finding one new claim and each judgement answering its
    question and raising `<question> more`. Returns the searches, the paths judged, the stream."""
    paths = []

    def predict(model, signature, **inputs):
        if signature is SearchFor:
            return type("Prediction", (), {"query": inputs["question"]})
        assert signature is Expand
        paths.append(inputs["path"])
        ids = [int(line[1:].split("]")[0]) for line in inputs["evidence"].splitlines()[::2]]
        return type(
            "Prediction",
            (),
            {
                "status": "answered",
                "claims": ids,
                "missing": "",
                "raised": [NamedQuestion(title="more", question=f"{inputs['question']} more")],
            },
        )

    def read(model, encoder, brief, question, hits, first_id, step, label, stream, checker):
        return [claim(first_id, f"https://site{first_id}.org")]

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
        {"plan": None, "intake": None, "check": None},
        encoder,
        Pages(),
        "2026-11-01",
        "brief",
        questions,
        claims,
        Frontier(encoder, "brief", claims),
        budget,
        1,
        3,
        max_depth,
        branch_searches,
        stream,
    )
    return searches, paths, stream


def test_a_judgement_reads_its_path_and_raises_children_down_to_the_depth_cap(monkeypatch):
    questions = [Question(id=1, text="a")]

    searches, paths, _ = explored(monkeypatch, questions, max_depth=1)

    # The child is judged under its parent, and at the depth cap raises nothing.
    assert searches == 2
    assert paths == ["none", "a"]
    assert [(q.text, q.parent, q.depth, q.status) for q in questions] == [
        ("a", None, 0, "answered"),
        ("a more", 1, 1, "answered"),
    ]


def test_a_branch_closes_once_it_has_spent_its_searches(monkeypatch):
    questions = [Question(id=1, text="a")]

    searches, _, stream = explored(monkeypatch, questions, branch_searches=2)

    assert searches == 2
    assert ("research", "closing the branch of a more: it spent its 2 searches") in stream.events
    # Raised before the branch closed, never searched.
    assert questions[-1].text == "a more more"
    assert questions[-1].queries == []


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
            "write": [{"text": "It was found in 1904 [1]."}],
            "verify": [{"supported": True, "reason": "the quote gives the year"}],
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
        "## Q1?\n\nIt was found in 1904 [1].\n\n## Open questions\n\n- Q2? (not found)"
    )
    assert [(q.title, q.text, q.parent, q.status) for q in result.questions] == [
        ("first", "Q1?", None, "answered"),
        ("second", "Q2?", None, "open"),
        ("third", "Q3?", 1, "open"),
    ]
    assert [c.url for c in result.citations] == ["https://a"]
