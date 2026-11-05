"""Scoring a research result, with the judge faked."""

from __future__ import annotations

import pytest
from dspy.utils.dummies import DummyLM

from kvasir.research import evaluate
from kvasir.research.evaluate import Question

QUESTION = Question(
    id="stone",
    question="When was the Kvasir stone found?",
    output="report",
    checks=["Gives the year it was found.", "Names who found it."],
)

RESULT = {
    "report": (
        "## Discovery\n\nIt was found in 1904 [1]. Nobody knows by whom. It weighs a tonne [2].\n\n"
        "| Option | Year |\n| --- | --- |\n| Stone | 1904 [1] |\n\n"
        "## Later\n\n- It moved to Oslo in 1920 [3]."
    ),
    "output": "report",
    "citations": [
        {"index": 1, "url": "https://a", "title": "A", "snippet": "s"},
        {"index": 2, "url": "https://b", "title": "B", "snippet": "s"},
    ],
    "claims": [
        {"id": 1, "text": "Found in 1904.", "quote": "found in 1904", "url": "https://a"},
        {"id": 2, "text": "Weighs 900 kg.", "quote": "900 kg", "url": "https://b"},
        {"id": 3, "text": "Unrelated.", "quote": "other", "url": "https://c"},
    ],
    "removed": [{"section": "Discovery", "sentence": "x", "reason": "y"}],
    "duration_seconds": 300.0,
}


def test_prose_skips_headings_and_tables():
    assert list(evaluate.prose(RESULT["report"])) == [
        "It was found in 1904 [1].",
        "Nobody knows by whom.",
        "It weighs a tonne [2].",
        "- It moved to Oslo in 1920 [3].",
    ]


def test_a_result_is_scored_on_checks_and_citations():
    judge = DummyLM(
        [
            {"met": True, "reason": "1904"},
            {"met": False, "reason": "no finder named"},
            {"verdict": "supported", "reason": "quoted"},
            {"verdict": "unsupported", "reason": "900 kg is not a tonne"},
        ]
    )

    score = evaluate.score(judge, QUESTION, RESULT)

    assert (score.checks_met, score.checks) == (1, 2)
    # [3] numbers no source the run listed, so it fails without asking the judge.
    assert (score.supported_sentences, score.cited_sentences) == (1, 3)
    assert score.uncited_sentences == 1
    assert score.removed_by_pipeline == 1
    assert score.failures == [
        "check: Names who found it. (no finder named)",
        "citation: It weighs a tonne [2]. (900 kg is not a tonne)",
        "citation: - It moved to Oslo in 1920 [3]. (cites no source the run listed)",
    ]


def test_a_citation_is_judged_against_the_claims_of_its_source():
    assert evaluate._evidence(RESULT, [2]) == "- Weighs 900 kg.\n    quote: 900 kg"


def test_an_unreadable_verdict_counts_against_the_report():
    judge = DummyLM([{"nonsense": "x"}] * 8)

    assert evaluate._ask(
        judge, evaluate.Check, "met", question="q", report="r", requirement="x"
    ) == (
        False,
        "the judge's verdict could not be read",
    )


def test_server_sent_events_are_read_as_pairs():
    lines = ["event: run", 'data: {"run_id": "r"}', "", "event: done", 'data: {"report": "x"}']

    assert list(evaluate.frames(lines)) == [("run", {"run_id": "r"}), ("done", {"report": "x"})]


def test_the_question_set_loads():
    questions = evaluate.load_questions(evaluate.QUESTIONS)

    assert len(questions) == 20
    assert len({q.id for q in questions}) == 20
    assert {q.output for q in questions} == {"report", "ranking", "table"}
    assert all(q.checks for q in questions)


def test_the_cli_stops_before_researching_when_setup_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("KVASIR_TOKEN", "token")
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path / "data"))
    researched = []
    monkeypatch.setattr(evaluate, "research", lambda *args: researched.append(args))

    with pytest.raises(SystemExit, match="model.fast"):
        evaluate.main([str(tmp_path / "out")])

    assert researched == []
