"""One signature per model step. Each call sees one small input, never the run's history."""

from __future__ import annotations

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel

from kvasir.deep.store import Confidence


class Clarify(dspy.Signature):
    """Before researching a question, decide what you would need to ask its author.

    Ask only what would change what you research or how you report it, such as scope, region,
    period, or what to compare. Ask at most three questions. Return none if the question is clear.
    """

    question: str = dspy.InputField()
    questions: list[str] = dspy.OutputField(desc="at most three short questions, or an empty list")


class Brief(dspy.Signature):
    """Turn a research question and its clarifications into a research brief.

    The brief states what to find out, the scope, and what a good answer contains. Record every
    choice the question and the clarifications left open as an assumption. A question about what
    is best, latest or current is about the state of things on the given date.
    """

    today: str = dspy.InputField(desc="today's date")
    question: str = dspy.InputField()
    clarifications: str = dspy.InputField(desc="questions asked and the answers given, or none")
    brief: str = dspy.OutputField(desc="one paragraph")
    assumptions: list[str] = dspy.OutputField(desc="choices made on the author's behalf")


class PlanQueries(dspy.Signature):
    """Plan the next web searches for a research brief.

    The notes say what is known and what is missing. Search for what is missing. Do not repeat a
    search the notes show was already done. When the brief is about the current state of things,
    name the current year in the query, since what you know may be out of date.
    """

    today: str = dspy.InputField(desc="today's date")
    brief: str = dspy.InputField()
    notes: str = dspy.InputField(desc="what is known so far, or empty at the start")
    count: int = dspy.InputField(desc="how many queries to return")
    queries: list[str] = dspy.OutputField(desc="search engine queries")


class Finding(BaseModel):
    claim: str
    quote: str
    confidence: Confidence


class Intake(dspy.Signature):
    """Extract the facts in a piece of a web page that help answer a research brief.

    Each claim is one self-contained fact. Its quote is copied exactly from the text, word for
    word, short but enough to support the claim. Return an empty list if nothing in the text helps.
    """

    brief: str = dspy.InputField()
    title: str = dspy.InputField()
    text: str = dspy.InputField(desc="a piece of the page")
    findings: list[Finding] = dspy.OutputField()


class RebuildNotes(dspy.Signature):
    """Rewrite the research notes to include new claims.

    Keep them short: what is established, with claim ids in brackets, what is still missing, and
    which lines of search led nowhere. Drop anything the brief does not need.
    """

    brief: str = dspy.InputField()
    notes: str = dspy.InputField(desc="the notes so far, or empty")
    new_claims: str = dspy.InputField(desc="one claim per line, prefixed by its id in brackets")
    notes_out: str = dspy.OutputField(desc="the rewritten notes, under 1000 words")


class Outline(dspy.Signature):
    """Outline a research report answering a brief from research notes.

    The first section gives the key findings. Each later section covers one part of the answer.
    """

    brief: str = dspy.InputField()
    notes: str = dspy.InputField()
    sections: list[str] = dspy.OutputField(desc="section titles, three to seven")


class WriteSection(dspy.Signature):
    """Write one section of a research report from its evidence.

    Use only the evidence. Cite every factual sentence with the bracketed ids of the claims that
    support it, such as [3] or [3][7]. Say plainly what the evidence does not settle. Do not repeat
    what earlier sections said. Do not write the section title. Mark a sub-section with a `###`
    heading, not a line of bold text.
    """

    brief: str = dspy.InputField()
    outline: str = dspy.InputField()
    section: str = dspy.InputField()
    earlier_sections: str = dspy.InputField(desc="the start of each section already written")
    evidence: str = dspy.InputField(desc="one claim per line, prefixed by its id in brackets")
    text: str = dspy.OutputField(desc="the section's paragraphs, in markdown")


class Verify(dspy.Signature):
    """Decide whether a sentence from a report is supported by the evidence it cites.

    Supported means every fact in the sentence is stated in the quotes, or follows directly from
    them. A sentence that adds a number, a name or a comparison the quotes do not contain is not
    supported. Wording may differ.
    """

    sentence: str = dspy.InputField()
    evidence: str = dspy.InputField(desc="each cited claim, with the quote it was read from")
    supported: bool = dspy.OutputField()
    reason: str = dspy.OutputField(desc="one short sentence")
