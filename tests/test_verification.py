"""Verification past the quote check: the grounding checker, corroboration, contradiction."""

from __future__ import annotations

import pytest
from dspy.utils.dummies import DummyLM

from fakes import Stream, Vectors, claim, research_settings
from kvasir.config import Settings
from kvasir.research import verify
from kvasir.research.corroborate import cross_check, domain, neighbours
from kvasir.research.ground import PROMPT, grounded
from kvasir.research.read import GAP, intake
from kvasir.runners import build_checker


class Checker:
    """A grounding checker answering from a list, recording each document and claim it was shown."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, prompt):
        self.asked.append(prompt)
        return [self.answers.pop(0)]


@pytest.mark.parametrize(("answer", "supported"), [("Yes", True), (" yes.", True), ("No", False)])
def test_the_checker_answers_yes_or_no(answer, supported):
    checker = Checker(answer)

    assert grounded(checker, "It was found in 1904.", "The stone was found in 1904.") is supported
    assert checker.asked == [
        PROMPT.format(document="It was found in 1904.", claim="The stone was found in 1904.")
    ]


def test_no_checker_is_built_unless_configured():
    assert build_checker(research_settings()) is None


def test_a_configured_checker_answers_at_temperature_zero():
    env = {
        "OPENAI_API_KEY": "key",
        "OPENAI_API_BASE": "https://gateway.example/v1",
        "KVASIR_MODEL_FAST": "openai/fast",
        "KVASIR_MODEL_STRONG": "openai/strong",
        "KVASIR_SEARXNG_URL": "http://searxng.example",
    }
    checker = build_checker(Settings.from_env(env | {"KVASIR_MODEL_CHECKER": "openai/minicheck"}))

    assert checker is not None
    assert checker.kwargs["temperature"] == 0.0
    with pytest.raises(Exception, match="must name a provider"):
        Settings.from_env(env | {"KVASIR_MODEL_CHECKER": "minicheck"})


def test_intake_drops_a_claim_its_piece_does_not_support():
    piece = "The stone was found in 1904. It weighs 900 kg."
    model = DummyLM(
        [
            {
                "findings": [
                    {"claim": "Found in 1904", "quote": "found in 1904", "confidence": "high"},
                    {"claim": "Weighs a tonne", "quote": "It weighs 900 kg", "confidence": "high"},
                ]
            }
        ]
    )
    checker = Checker("Yes", "No")
    hit = {"url": "https://a", "title": "A"}

    claims = intake(model, "brief", "question", hit, piece, 1, 1, checker)

    assert [c.text for c in claims] == ["Found in 1904"]
    # The whole piece is the document, since a claim names what its quote leaves to the page.
    assert checker.asked[0] == PROMPT.format(document=piece, claim="Found in 1904")


def test_the_checker_decides_every_cited_sentence_against_all_it_cites():
    claims = {1: claim(1, "https://a"), 2: claim(2, "https://b")}
    checker = Checker("No", "Yes")
    # Never asked: the checker decides every sentence.
    model = DummyLM([])

    sections, removed = verify.report(
        model, [("S", "One [1]. Both [1][2].")], claims, Stream(), checker
    )

    assert sections == [("S", "Both [1][2].")]
    assert [r.sentence for r in removed] == ["One [1]."]
    assert checker.asked == [
        PROMPT.format(document="claim 1\nq", claim="One."),
        PROMPT.format(document="claim 1\nq\n\nclaim 2\nq", claim="Both."),
    ]


def test_neighbours_are_close_claims_from_other_sites():
    a, b, c, d = (
        claim(1, "https://www.a.org/x"),
        claim(2, "https://a.org/y"),
        claim(3, "https://b.org"),
        claim(4, "https://c.org"),
    )
    encoder = Vectors(
        {"claim 1": [1, 0], "claim 2": [1, 0], "claim 3": [0.9, 0.1], "claim 4": [0, 1]}
    )

    pairs = sorted((x.id, y.id) for x, y in neighbours(encoder, [a, b, c, d]))

    # The same site, once `www.` is dropped, is no corroboration; claim 4 is about another thing.
    assert pairs == [(1, 3), (2, 3), (3, 1), (3, 2)]
    assert domain("https://www.A.org/x") == "a.org"


def test_cross_checking_records_support_and_disagreement():
    a, b = claim(1, "https://a.org"), claim(2, "https://b.org")
    encoder = Vectors({"claim 1": [1, 0], "claim 2": [1, 0]})
    # Claim 2 supports claim 1, not the other way; the second pair is then asked about disagreeing.
    models = {
        "ground": Checker("Yes", "No"),
        "contradict": DummyLM([{"disagree": True}]),
    }

    cross_check(models, encoder, [a, b], Stream(), corroborate=True, contradict=True)

    assert a.corroborated_by == [2]
    assert b.corroborated_by == []
    assert a.contradicted_by == [2]
    assert b.contradicted_by == [1]


def test_cross_checking_asks_nothing_unless_asked_to():
    a, b = claim(1, "https://a.org"), claim(2, "https://b.org")

    cross_check({}, None, [a, b], Stream(), corroborate=False, contradict=False)

    assert a.corroborated_by == b.corroborated_by == []


def test_a_claim_is_grounded_on_the_piece_its_quote_is_in():
    first, second = "The stone was found in 1904.", "It weighs 900 kg."
    model = DummyLM(
        [
            {
                "findings": [
                    {"claim": "Weighs 900 kg", "quote": "It weighs 900 kg", "confidence": "high"}
                ]
            }
        ]
    )
    checker = Checker("Yes")
    hit = {"url": "https://a", "title": "A"}

    claims = intake(model, "brief", "question", hit, f"{first}{GAP}{second}", 1, 1, checker)

    assert [c.text for c in claims] == ["Weighs 900 kg"]
    assert checker.asked[0] == PROMPT.format(document=second, claim="Weighs 900 kg")
