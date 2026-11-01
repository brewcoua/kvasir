"""The frontier: the worth of each question, merging repeated claims, closing a spent branch."""

from __future__ import annotations

from fakes import Vectors, claim
from kvasir.methods.ledger.frontier import BRANCH_STALL, Frontier
from kvasir.methods.ledger.questions import Question


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
