"""The graph a run builds: questions, the claims answering them, and the questions claims raise.

Question nodes are the ledger's `Question`. A question's `parent` is the question whose evidence
raised it, and its `claims` the claims answering it. Claims corroborate or contradict each other
through their own `corroborated_by` and `contradicted_by`. A branch is a first question and every
question raised under it.
"""

from __future__ import annotations

from kvasir.methods.ledger.frontier import roots
from kvasir.methods.ledger.questions import Question


def path(questions: list[Question], question: Question) -> list[Question]:
    """The questions `question` was raised under, the first question first."""
    by_id = {q.id: q for q in questions}
    above = []
    parent = question.parent
    while parent is not None:
        above.append(by_id[parent])
        parent = by_id[parent].parent
    return above[::-1]


def branches(questions: list[Question]) -> dict[int, list[Question]]:
    """Each first question's id, and the questions of its branch, itself first."""
    of = roots(questions)
    grouped: dict[int, list[Question]] = {}
    for question in questions:
        grouped.setdefault(of[question.id], []).append(question)
    return grouped
