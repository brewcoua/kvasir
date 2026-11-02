"""The signatures only the graph method uses."""

from __future__ import annotations

from typing import Literal

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from kvasir.methods.ledger.signatures import NamedQuestion


class Expand(dspy.Signature):
    """Decide how well the evidence answers one research question, and what it raises next.

    The path is the chain of questions this one was asked under, from the first. Count a claim
    only if its quote states what the claim says. answered: the counted claims settle the
    question. weak: they settle part of it. contradicted: they disagree with each other. open: no
    claim speaks to it. Then ask the questions the evidence itself raises: something a claim names
    without explaining, a figure it gives without a source or a date, a point two claims disagree
    on. Unless it is answered, ask one for each separate part of what is still missing: a question
    nearly answered raises one or none, a hard one up to four. When it is answered, ask the one
    harder question that would take the answer deeper. Ask none the path already asks.
    """

    brief: str = dspy.InputField()
    path: str = dspy.InputField(desc="the questions above this one, first first, or none")
    question: str = dspy.InputField()
    evidence: str = dspy.InputField(desc="each claim with the quote it was read from")
    status: Literal["answered", "weak", "contradicted", "open"] = dspy.OutputField()
    claims: list[int] = dspy.OutputField(desc="ids of the claims counted as support")
    missing: str = dspy.OutputField(desc="what is still missing, in a few words, or empty")
    raised: list[NamedQuestion] = dspy.OutputField(
        desc="at most four questions, one when answered, or empty"
    )


class Gaps(dspy.Signature):
    """Name what a research report answering a brief still lacks, given every question asked so far.

    Ask a new question only for a part of the brief no question below covers, or for one left
    unanswered, from an angle its own question did not take. Ask none that repeats a question
    below. Return none when the questions already cover the brief.
    """

    brief: str = dspy.InputField()
    questions: str = dspy.InputField(desc="one question per line: name: question (status)")
    raised: list[NamedQuestion] = dspy.OutputField(desc="at most three new questions, or empty")


class Section(BaseModel):
    """One section of the report, and the questions whose findings it reports."""

    title: str = Field(description="two to six words, a heading a reader scans, not a question")
    questions: list[int] = Field(description="ids of the questions it reports on")
    subsections: list[str] = Field(
        default_factory=list,
        description="two to four subheadings when the section covers distinct parts, else empty",
    )


class Outline(dspy.Signature):
    """Outline a research report answering a brief, from the questions the research settled.

    Each section reports on one theme, under a short heading. Group the questions that bear on one
    theme, whichever first question they were raised under; a question may serve two sections.
    Order the sections as the answer reads best: what answers the brief first, then what explains,
    qualifies or disputes it. Give every question at least one section. Split a section that
    covers distinct parts into subsections, under headings as short as the sections'. No two
    sections share a title.
    """

    brief: str = dspy.InputField()
    questions: str = dspy.InputField(
        desc="one question per line: [id] name: question (status, claims found); what is missing"
    )
    sections: list[Section] = dspy.OutputField(desc="three to seven sections")
