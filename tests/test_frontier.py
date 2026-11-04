"""The frontier: the worth of each question, merging repeated claims, closing a spent branch."""

from __future__ import annotations

from fakes import Vectors, claim
from kvasir.methods.ledger.frontier import BRANCH_STALL, Frontier
from kvasir.methods.ledger.questions import Question, add_questions
from kvasir.methods.ledger.signatures import NamedQuestion


def test_a_repeat_from_another_site_adds_a_source_and_from_the_same_site_nothing():
    held = claim(1, "https://a.org/1")
    claims = [held]
    encoder = Vectors(
        {
            "brief": [1, 1],
            "claim 1": [1, 0],
            "claim 2": [1, 0],
            "claim 3": [1, 0],
            "claim 4": [0, 1],
        }
    )
    frontier = Frontier(encoder, "brief", claims)

    same_site, other_site, new = (
        claim(2, "https://www.a.org/2"),
        claim(3, "https://b.org"),
        claim(4, "https://c.org"),
    )
    claims.append(same_site)
    assert frontier.merge([same_site]) == 0
    claims.extend([other_site, new])
    assert frontier.merge([other_site, new]) == 2
    assert held.corroborated_by == [3]


def test_a_repeat_from_a_subdomain_or_with_a_copied_quote_adds_no_source():
    held = claim(1, "https://a.org/1", "The stone was found in the old quarry in 1904.")
    claims = [held]
    encoder = Vectors({"brief": [1, 1], "claim 1": [1, 0], "claim 2": [1, 0], "claim 3": [1, 0]})
    frontier = Frontier(encoder, "brief", claims)

    subdomain = claim(2, "https://blog.a.org/2")
    copied = claim(
        3, "https://b.org", "  the STONE was found in the\nold quarry in 1904. It is old. "
    )
    claims.extend([subdomain, copied])

    assert frontier.merge([subdomain, copied]) == 0
    assert held.corroborated_by == []
    assert len(claims) == 3


def test_a_branch_that_adds_nothing_is_closed_and_its_follow_ups_skipped():
    first = Question(id=1, text="first")
    follow_up = Question(id=2, text="follow-up", parent=1, depth=1)
    other = Question(id=3, text="other")
    questions = [first, follow_up, other]
    claims = []
    frontier = Frontier(Vectors({}, default=[1, 0]), "brief", claims)

    for searches in range(1, BRANCH_STALL + 1):
        closed = frontier.searched(questions, follow_up, [])
        assert closed is (searches == BRANCH_STALL)

    assert frontier.pick(questions, max_attempts=4) is other


def test_a_question_far_from_what_is_held_is_worth_more():
    claims = [claim(1, "https://a.org")]
    encoder = Vectors(
        {"brief": [1, 1], "claim 1": [1, 0], "known": [1, 0], "unknown": [0, 1]}, default=[1, 1]
    )
    frontier = Frontier(encoder, "brief", claims)
    known, unknown = Question(id=1, text="known"), Question(id=2, text="unknown")

    assert frontier.pick([known, unknown], max_attempts=4) is unknown


def test_depth_and_attempts_lower_the_worth():
    frontier = Frontier(Vectors({}, default=[1, 0]), "brief", [])
    shallow = Question(id=1, text="a")
    deep = Question(id=2, text="b", parent=1, depth=2)
    tried = Question(id=3, text="c", queries=["q"])

    assert frontier.worth(shallow) > frontier.worth(deep)
    assert frontier.worth(shallow) > frontier.worth(tried)


def test_a_more_central_question_is_worth_more():
    frontier = Frontier(Vectors({}, default=[1, 0]), "brief", [])
    core = Question(id=1, text="a", priority="core")
    peripheral = Question(id=2, text="b", priority="peripheral")

    assert frontier.worth(core) > frontier.worth(peripheral)


def test_a_follow_up_is_no_more_central_than_its_question():
    questions: list[Question] = []
    encoder = Vectors({"a": [1, 0], "b": [0, 1]})
    add_questions(
        encoder, questions, [NamedQuestion(title="a", question="a", priority="peripheral")]
    )

    add_questions(
        encoder, questions, [NamedQuestion(title="b", question="b", priority="core")], questions[0]
    )

    assert [q.priority for q in questions] == ["peripheral", "peripheral"]
    # A word outside the three fails no reply.
    assert NamedQuestion(title="c", question="c", priority="high").priority == "supporting"


def test_dropping_a_site_removes_its_claims_and_every_reference_to_them():
    claims = [claim(1, "https://a.org/1"), claim(2, "https://bad.org/2"), claim(3, "https://c.org")]
    claims[0].corroborated_by = [2, 3]
    claims[2].contradicted_by = [2]
    answered = Question(id=1, text="q", status="answered", claims=[2, 3])
    lost = Question(id=2, text="r", status="answered", claims=[2])
    frontier = Frontier(Vectors({}, default=[1, 0]), "brief", claims)
    frontier.held()

    assert frontier.drop({"bad.org"}, [answered, lost]) == {2}

    assert [c.id for c in claims] == [1, 3]
    assert len(frontier.held()) == 2
    assert claims[0].corroborated_by == [3] and claims[1].contradicted_by == []
    assert answered.claims == [3] and answered.status == "answered"
    assert lost.claims == [] and lost.status == "open"
