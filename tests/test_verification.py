"""Verification past the quote check: the grounding checker, corroboration, contradiction."""

from __future__ import annotations

import pytest
from dspy.utils.dummies import DummyLM

from fakes import Encoder, Logged, Stream, Tiers, Vectors, claim, page, research_settings
from kvasir.config import Settings
from kvasir.research import verify
from kvasir.research.corpus import Corpus
from kvasir.research.corroborate import copies, cross_check, domain, neighbours, registrable
from kvasir.research.ground import PROMPT, grounded
from kvasir.research.read import GAP, intake, read, supported
from kvasir.runners import build_checker
from kvasir.storm import runtime


class Checker:
    """A grounding checker answering from a list, recording each document and claim it was shown."""

    def __init__(self, *answers, log=None):
        self.answers = list(answers)
        self.asked = []
        self.log = log if log is not None else []

    def __call__(self, prompt):
        self.asked.append(prompt)
        self.log.append("checker")
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
        "KVASIR_PROVIDER_GATEWAY_API_KEY": "key",
        "KVASIR_PROVIDER_GATEWAY_API_BASE": "https://gateway.example/v1",
        "KVASIR_MODEL_FAST": "gateway/fast",
        "KVASIR_MODEL_STRONG": "gateway/strong",
        "KVASIR_MODEL_EMBEDDING": "gateway/embed",
        "KVASIR_SEARCH_URL": "http://searxng.example",
    }
    checker = build_checker(Settings.from_env(env | {"KVASIR_MODEL_CHECKER": "gateway/minicheck"}))

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

    claims = supported(checker, piece, intake(model, "brief", "question", hit, piece, 1, 1))

    assert [c.text for c in claims] == ["Found in 1904"]
    # The whole piece is the document, since a claim names what its quote leaves to the page.
    assert checker.asked[0] == PROMPT.format(document=piece, claim="Found in 1904")


def test_reading_grounds_the_claims_once_every_page_is_read(monkeypatch):
    monkeypatch.setattr(
        "kvasir.research.read.fetch_page", lambda url: page("It was found in 1904.")
    )
    log = []
    found = {"findings": [{"claim": "Found", "quote": "found in 1904", "confidence": "high"}]}
    model = Logged("intake", log, [found, found])
    checker = Checker("Yes", "No", log=log)
    hits = [
        {"url": "https://a", "title": "A", "description": ""},
        {"url": "https://b", "title": "B", "description": ""},
    ]

    claims = read(
        model, Encoder(), "brief", "q", hits, 5, 1, "search 1", Stream(), Corpus(), checker
    )

    assert log == ["intake", "intake", "checker", "checker"]
    assert [(c.id, c.url) for c in claims] == [(5, "https://a")]


def test_the_checker_decides_every_cited_sentence_against_all_it_cites():
    claims = {1: claim(1, "https://a"), 2: claim(2, "https://b")}
    checker = Checker("Yes", "Yes")
    # Never asked: the checker keeps both.
    models = {"ground": checker, "verify": DummyLM([]), "escalate": DummyLM([])}

    checked = verify.report(models, [("S", "One [1]. Both [1][2].")], claims, Stream())

    assert checked.sections == [("S", "One [1]. Both [1][2].")]
    assert checker.asked == [
        PROMPT.format(document="claim 1\nquote #1.", claim="One."),
        PROMPT.format(document="claim 1\nquote #1.\n\nclaim 2\nquote #2.", claim="Both."),
    ]


def run(sentence="Fact [1].", *, checker=(), fast=(), reason=(), claims=None):
    """The report of one section holding `sentence`, with each model answering as told, and the
    order in which the models were called."""
    log = []
    models = {
        "verify": Logged("fast", log, fast),
        "escalate": Logged("escalate", log, reason),
    }
    if checker:
        models["ground"] = Checker(*checker, log=log)
    claims = claims or {1: claim(1, "https://a")}
    return verify.report(models, [("S", sentence)], claims, Stream()), log


def answer(verdict, reason="because", rewrite=""):
    return {"verdict": verdict, "reason": reason, "rewrite": rewrite}


def test_a_sentence_citing_no_known_claim_is_removed_without_a_call():
    checked, log = run("Invented [9].")

    assert checked.sections == [("S", "")]
    assert [(r.stage, r.reason) for r in checked.removed] == [
        ("citation", "cites no claim the research gathered")
    ]
    assert log == []


def test_a_sentence_the_checker_supports_never_reaches_the_fast_model():
    checked, log = run(checker=["Yes"])

    assert checked.sections == [("S", "Fact [1].")]
    assert log == ["checker"]


def test_a_sentence_the_checker_and_the_fast_model_reject_is_removed():
    checked, log = run(checker=["No"], fast=[answer("unsupported", "not in the quote")])

    assert checked.sections == [("S", "")]
    assert [(r.stage, r.reason) for r in checked.removed] == [("verify", "not in the quote")]
    assert log == ["checker", "fast"]


def test_a_sentence_the_fast_model_supports_against_the_checker_goes_to_the_reason_model():
    checked, log = run(checker=["No"], fast=[answer("supported")], reason=[answer("supported")])

    assert checked.sections == [("S", "Fact [1].")]
    assert log == ["checker", "fast", "escalate"]


def test_the_reason_model_may_remove_a_sentence():
    checked, _ = run(
        checker=["No"], fast=[answer("partial")], reason=[answer("unsupported", "adds a date")]
    )

    assert checked.sections == [("S", "")]
    assert [(r.stage, r.reason) for r in checked.removed] == [("escalate", "adds a date")]


def test_a_partial_sentence_is_rewritten_when_the_rewrite_passes_the_checker():
    rewrite = "Fact [1]."
    checked, log = run(
        "Fact in 1904 [1].",
        checker=["No", "Yes"],
        fast=[answer("partial")],
        reason=[answer("partial", "the year is not quoted", rewrite)],
    )

    assert checked.sections == [("S", rewrite)]
    assert [(r.before, r.after, r.reason) for r in checked.revised] == [
        ("Fact in 1904 [1].", rewrite, "the year is not quoted")
    ]
    assert checked.removed == []
    assert log == ["checker", "fast", "escalate", "checker"]


def test_a_rewrite_without_a_checker_is_checked_by_the_fast_model():
    checked, log = run(
        fast=[answer("partial"), answer("supported")],
        reason=[answer("partial", rewrite="**Fact** [1].")],
    )

    assert checked.sections == [("S", "**Fact** [1].")]
    assert log == ["fast", "escalate", "fast"]


def test_a_rewrite_the_checker_rejects_is_removed():
    checked, _ = run(
        checker=["No", "No"],
        fast=[answer("partial")],
        reason=[answer("partial", rewrite="Less [1].")],
    )

    assert checked.sections == [("S", "")]
    assert [r.stage for r in checked.removed] == ["rewrite"]
    assert checked.revised == []


def test_a_rewrite_citing_a_new_claim_is_removed_unchecked():
    claims = {1: claim(1, "https://a"), 2: claim(2, "https://b")}
    checked, log = run(
        checker=["No"],
        fast=[answer("partial")],
        reason=[answer("partial", rewrite="Fact [1][2].")],
        claims=claims,
    )

    assert checked.sections == [("S", "")]
    assert [r.stage for r in checked.removed] == ["rewrite"]
    assert log == ["checker", "fast", "escalate"]


def test_a_rewrite_citing_nothing_is_removed_unchecked():
    checked, log = run(
        checker=["No"], fast=[answer("partial")], reason=[answer("partial", rewrite="Fact.")]
    )

    assert checked.sections == [("S", "")]
    assert [(r.stage, r.reason) for r in checked.removed] == [
        ("rewrite", "the rewrite cites no claim")
    ]
    assert log == ["checker", "fast", "escalate"]


def test_a_partial_verdict_without_a_rewrite_removes_the_sentence():
    checked, _ = run(fast=[answer("partial")], reason=[answer("partial")])

    assert [r.stage for r in checked.removed] == ["rewrite"]


def test_a_verdict_that_cannot_be_read_leaves_the_sentence_marked():
    checked, _ = run(
        "Fact [1]. Other [1].",
        fast=[answer("partial"), answer("supported")],
        reason=[{"nonsense": "x"}] * 4,
    )

    assert checked.sections == [("S", f"Fact [1]. {verify.UNVERIFIED} Other [1].")]
    assert [u.sentence for u in checked.unverified] == ["Fact [1]."]
    assert checked.removed == []


def test_a_sentence_left_unverified_still_counts_as_checked():
    stream = Stream()
    models = {
        "verify": Logged("fast", [], [answer("partial"), answer("supported")]),
        "escalate": Logged("escalate", [], [{"nonsense": "x"}] * 4),
    }

    verify.report(models, [("S", "Fact [1]. Other [1].")], {1: claim(1, "https://a")}, stream)

    checked = [detail for _, detail in stream.events if detail.startswith("checked ")]
    assert checked[-1] == "checked 2 of 2 cited sentences"


def test_without_a_checker_the_fast_model_keeps_a_supported_sentence_and_alone():
    checked, log = run(fast=[answer("supported")])

    assert checked.sections == [("S", "Fact [1].")]
    assert log == ["fast"]


def test_every_checker_call_comes_before_the_first_fast_call():
    log = []
    models = {
        "ground": Checker("No", "Yes", "No", log=log),
        "verify": Logged("fast", log, [answer("unsupported"), answer("unsupported")]),
        "escalate": Logged("escalate", log),
    }
    claims = {1: claim(1, "https://a")}

    checked = verify.report(
        models, [("A", "One [1]. Two [1]."), ("B", "Three [1].")], claims, Stream()
    )

    assert log == ["checker"] * 3 + ["fast"] * 2
    assert checked.sections == [("A", "Two [1]."), ("B", "")]


def test_the_unverified_mark_stays_with_its_sentence_and_is_not_a_claim():
    line = f"One [1]. {verify.UNVERIFIED} Two [2]. {verify.UNVERIFIED} Three."

    assert verify.sentences(line) == [
        f"One [1]. {verify.UNVERIFIED}",
        f"Two [2]. {verify.UNVERIFIED}",
        "Three.",
    ]
    assert verify.cited(line) == [1, 2]
    assert verify.unmarked(f"One [1]. {verify.UNVERIFIED}") == "One."


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


def test_neighbours_are_not_subdomains_of_one_site_nor_a_copied_quote():
    a, b, c, d = (
        claim(1, "https://a.org", "The stone was found in the old quarry in 1904."),
        claim(2, "https://blog.a.org", "Another quote."),
        claim(3, "https://b.org", "the stone was  FOUND in the old quarry in 1904. Old."),
        claim(4, "https://c.org"),
    )
    encoder = Vectors({f"claim {i}": [1, 0] for i in range(1, 5)})

    pairs = sorted((x.id, y.id) for x, y in neighbours(encoder, [a, b, c, d]))

    assert pairs == [(1, 4), (2, 3), (2, 4), (3, 2), (3, 4), (4, 1), (4, 2), (4, 3)]
    assert registrable("https://a.org:8080/x") == registrable("https://blog.a.org") == "a.org"
    assert registrable("http://127.0.0.1/x") == "127.0.0.1"
    assert registrable("http://localhost/x") == "localhost"
    assert not copies(claim(5, "https://d.org", "in 1904"), a)
    assert not copies(claim(6, "https://d.org", ""), a)


def test_cross_checking_records_support_and_disagreement():
    a, b = claim(1, "https://a.org"), claim(2, "https://b.org")
    encoder = Vectors({"claim 1": [1, 0], "claim 2": [1, 0]})
    models = {"ground": Checker("No", "No"), "contradict": DummyLM([{"disagree": [2]}])}

    cross_check(models, encoder, [a, b], Stream(), corroborate=True, contradict=True)

    assert a.corroborated_by == b.corroborated_by == []
    assert a.contradicted_by == [2]
    assert b.contradicted_by == [1]


def test_claims_one_supports_are_not_asked_about_disagreeing():
    a, b = claim(1, "https://a.org"), claim(2, "https://b.org")
    encoder = Vectors({"claim 1": [1, 0], "claim 2": [1, 0]})
    # Claim 2 supports claim 1, not the other way.
    contradict = DummyLM([])
    models = {"ground": Checker("Yes", "No"), "contradict": contradict}

    cross_check(models, encoder, [a, b], Stream(), corroborate=True, contradict=True)

    assert a.corroborated_by == [2]
    assert b.corroborated_by == []
    assert a.contradicted_by == b.contradicted_by == []
    assert contradict.history == []


def test_support_is_checked_for_every_pair_before_disagreement(monkeypatch):
    # One thread, so the fake model's answers go to the claims in order.
    monkeypatch.setattr(runtime, "_max_threads", 1)
    a, b, c = claim(1, "https://a.org"), claim(2, "https://b.org"), claim(3, "https://c.org")
    encoder = Vectors({"claim 1": [1, 0], "claim 2": [1, 0], "claim 3": [1, 0]})
    log = []
    # Claim 1 is asked about 2 and 3 in one call, claim 2 about 3; 9 was never shown.
    contradict = Logged("contradict", log, [{"disagree": [3, 9]}, {"disagree": []}])
    models = {"ground": Checker(*["No"] * 6, log=log), "contradict": contradict}

    cross_check(models, encoder, [a, b, c], Stream(), corroborate=True, contradict=True)

    assert log == ["checker"] * 6 + ["contradict"] * 2
    assert a.contradicted_by == [3]
    assert b.contradicted_by == []
    assert c.contradicted_by == [1]


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

    piece = f"{first}{GAP}{second}"

    claims = supported(checker, piece, intake(model, "brief", "question", hit, piece, 1, 1))

    assert [c.text for c in claims] == ["Weighs 900 kg"]
    assert checker.asked[0] == PROMPT.format(document=second, claim="Weighs 900 kg")


def test_a_sentence_whose_claims_all_come_from_low_sites_is_marked():
    claims = [
        claim(1, "https://low.org/1"),
        claim(2, "https://low.org/2"),
        claim(3, "https://a.org"),
    ]
    low = Corpus(sources=Tiers(low_org="low")).low_trust(claims)
    sections = [
        (
            "S",
            "Both low [1][2]. Mixed [1, 3]. Fine [3]. A transition. Unknown claim [9].",
        )
    ]

    found = verify.low_trust(sections, {c.id: c for c in claims}, low)

    assert [(m.section, m.sentence) for m in found] == [("S", "Both low [1][2].")]
    assert Corpus().low_trust(claims) == set()
