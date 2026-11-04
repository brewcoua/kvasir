"""Which question to search next: the one that settles the most of the brief for the least work.

This is proof-number search (Allis 1994) over the question graph. A question is settled by
searching it again, or by settling every question it raised: an OR of a retry and an AND of its
children. Its work is the fewest searches either way would take, as estimated here: none once
answered, one for a question never searched, one more than its attempts for a retry, since each
failed search makes the next less likely to succeed, and the sum of its children's work for the
children. Children that are all answered did not settle a question still judged unanswered, so
they are no way to settle it. A question in a closed branch never settles.

An answered question is settled, so it counts as no work in its parent's split. Its own work, as
that of a question that can be neither retried nor split, is what is left below it: the work of
the questions it raised that can still be settled. So the run still reaches the follow-ups of an
answered question, and the children of a question given up.

The run takes the first question with the most worth per unit of work, then walks down it to the
question whose search settles the most for the least: at each level, the question itself while a
retry costs less than its children, else its child with the least work.

Two rules put the first questions ahead, since they are what the brief asks and what the report is
outlined from. A first question not yet answered after one search is searched again before the run
goes two levels below any first question. Late in the research, a first question that can still be
settled is searched before any question raised below one.
"""

from __future__ import annotations

import math

from kvasir.methods.ledger.frontier import Frontier, roots
from kvasir.methods.ledger.questions import Question

NEVER = math.inf
# Searches a first question not yet answered gets before the run goes two levels below any.
FIRST_SEARCHES = 2


def children(questions: list[Question]) -> dict[int, list[Question]]:
    """Each question's id, and the questions it raised."""
    below: dict[int, list[Question]] = {q.id: [] for q in questions}
    for question in questions:
        if question.parent is not None:
            below[question.parent].append(question)
    return below


def retry(question: Question, max_attempts: int) -> float:
    """The work of searching `question` again."""
    if question.status in ("answered", "unresolved") or question.attempts >= max_attempts:
        return NEVER
    return 1 + question.attempts


def split(below: list[Question], of: dict[int, float]) -> float:
    """The work of settling every question in `below`, the children of one question."""
    if all(c.status == "answered" for c in below):
        return NEVER
    return sum(0 if c.status == "answered" else of[c.id] for c in below)


def work(questions: list[Question], max_attempts: int, closed: set[int]) -> dict[int, float]:
    """Each question's id, and the searches still needed to settle it."""
    root_of = roots(questions)
    below = children(questions)
    of: dict[int, float] = {}
    # A question is always added after the one that raised it, so children come first here.
    for question in reversed(questions):
        if root_of[question.id] in closed:
            of[question.id] = NEVER
            continue
        left = sum(of[c.id] for c in below[question.id] if of[c.id] < NEVER)
        if question.status == "answered":
            of[question.id] = left
            continue
        settle = min(retry(question, max_attempts), split(below[question.id], of))
        of[question.id] = settle if settle < NEVER else left or NEVER
    return of


def pick(
    questions: list[Question], frontier: Frontier, max_attempts: int, late: bool = False
) -> Question | None:
    """The question to search next, or None when no question can still be settled.

    `late` when little of the research time is left.
    """
    of = work(questions, max_attempts, frontier.closed)
    below = children(questions)
    first = [q for q in questions if q.parent is None and 0 < of[q.id] < NEVER]
    if not first:
        return None
    behind = [
        q
        for q in first
        if retry(q, max_attempts) < NEVER and (late or 0 < q.attempts < FIRST_SEARCHES)
    ]
    if late and behind:
        return max(behind, key=lambda q: (frontier.worth(q), -q.id))
    # Worth is positive for any question close to the brief; one far from it counts as none.
    question = max(first, key=lambda q: (max(frontier.worth(q), 0.0) / of[q.id], -of[q.id], -q.id))
    while True:
        open_below = [c for c in below[question.id] if 0 < of[c.id] < NEVER]
        # A tie goes to the children: they are what is left to find out about the question.
        if not open_below or retry(question, max_attempts) < split(below[question.id], of):
            return question
        if question.depth >= 1 and behind:
            return max(behind, key=lambda q: (frontier.worth(q), -q.id))
        question = min(open_below, key=lambda c: (of[c.id], -frontier.worth(c), c.id))
