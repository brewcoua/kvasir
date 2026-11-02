"""The ledger: the questions the research has to settle, and how far each got."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from kvasir.methods.ledger.signatures import NamedQuestion
from kvasir.schemas.results import ResearchQuestion
from kvasir.storm.encoder import Encoder, cosine_similarity

# `open`: nothing found yet. `weak`: part of an answer. `contradicted`: sources disagree.
# `unresolved`: every query shape was tried and found nothing.
Status = Literal["open", "weak", "answered", "contradicted", "unresolved"]

# Questions in the ledger at most. Follow-ups past this are dropped: the budget could not search
# for them anyway.
MAX_QUESTIONS = 24
# Above this cosine similarity, a new question is taken to repeat one in the ledger.
DUPLICATE_QUESTION = 0.9

# How the report names a question's status when the research did not settle it.
UNSETTLED = {
    "open": "not found",
    "unresolved": "not found",
    "weak": "partly answered",
    "contradicted": "sources disagree",
}


@dataclass
class Question:
    """One thing the research has to find out, and how far it got."""

    id: int
    text: str
    # A few words naming it, when the model that asked it gave one.
    title: str = ""
    # The question this one follows up on, one level shallower.
    parent: int | None = None
    depth: int = 0
    status: Status = "open"
    # The claims whose quotes support the answer, as the last judgement found them.
    claims: list[int] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    # What the last judgement found missing, for the next query to look for.
    missing: str = ""
    # How many claims the store held at the last judgement, so a question is judged again only
    # once there is something new to judge it on.
    judged_at: int = 0
    # For a ranking or a table: the option and the criterion of the empty cell this asks about.
    cell: tuple[str, str] | None = None

    @property
    def attempts(self) -> int:
        return len(self.queries)


def add_questions(
    encoder: Encoder,
    ledger: list[Question],
    asked: list[NamedQuestion],
    parent: Question | None = None,
    limit: int = MAX_QUESTIONS,
) -> None:
    """Add to `ledger` those of `asked` that repeat no question in it, up to `limit`."""
    asked = [q for q in asked if q.question.strip()]
    if not asked:
        return
    texts = [q.question.strip() for q in asked]
    vectors = list(encoder.encode([q.text for q in ledger] + texts))
    kept = vectors[: len(ledger)]
    for named, text, vector in zip(asked, texts, vectors[len(ledger) :], strict=True):
        if len(ledger) >= limit:
            return
        if kept and cosine_similarity([vector], kept).max() > DUPLICATE_QUESTION:
            continue
        ledger.append(
            Question(
                id=len(ledger) + 1,
                text=text,
                title=named.title.strip(),
                parent=parent.id if parent else None,
                depth=parent.depth + 1 if parent else 0,
            )
        )
        kept.append(vector)


def notes(ledger: list[Question]) -> str:
    """The ledger as the outline reads it: each question, how far it got, and its claims."""
    lines = []
    for question in ledger:
        if question.cell:
            continue
        line = f"- {question.text} ({question.status})"
        if question.claims:
            line += " " + "".join(f"[{i}]" for i in question.claims)
        if question.missing:
            line += f"; missing: {question.missing}"
        lines.append(line)
    return "\n".join(lines)


def open_questions(ledger: list[Question]) -> str:
    """The questions the research did not settle, as a list for the end of the report.

    Empty cells are left out: the table already shows them as a dash. So are follow-ups the budget
    never reached, which were only suggestions; a first question it never reached says so.
    """
    return "\n".join(
        f"- {q.text} ({UNSETTLED[q.status] if q.attempts else 'not searched'})"
        for q in ledger
        if q.status in UNSETTLED and q.cell is None and (q.attempts or q.depth == 0)
    )


def as_results(ledger: list[Question]) -> list[ResearchQuestion]:
    return [
        ResearchQuestion(
            id=q.id,
            text=q.text,
            title=q.title,
            parent=q.parent,
            depth=q.depth,
            status=q.status,
            claims=q.claims,
            queries=q.queries,
            missing=q.missing,
        )
        for q in ledger
    ]
