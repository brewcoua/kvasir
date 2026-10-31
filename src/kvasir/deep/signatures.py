"""One signature per model step. Each call sees one small input, never the run's history."""

from __future__ import annotations

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel

from kvasir.deep.store import Confidence
from kvasir.models import Output


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
    output: Output = dspy.OutputField(
        desc="ranking if the question asks which options are best, table if it asks to compare "
        "options without ranking them, report otherwise"
    )
    criteria: list[str] = dspy.OutputField(
        desc="the criteria the question says to compare or rank on, in its words, or empty"
    )
    entities: list[str] = dspy.OutputField(
        desc="the options the question names as the full set to compare, or empty if it leaves "
        "the set open"
    )


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


class ChosenCriterion(BaseModel):
    name: str
    weight: int


class Criteria(dspy.Signature):
    """Choose the criteria to compare options on, for a research brief.

    Include every criterion the brief names. Each is one thing a source can state about an option,
    such as a size, a price or a feature, not a judgement such as "overall quality". Weigh each
    from 1 to 3 by how much it matters to the brief.
    """

    brief: str = dspy.InputField()
    background: str = dspy.InputField(desc="search results on the topic")
    criteria: list[ChosenCriterion] = dspy.OutputField(desc="three to five criteria")


class Entities(dspy.Signature):
    """List the options a research brief compares, such as products, companies or models.

    Include every option the brief names. Then add the options the search results show are the
    main ones. An option is something the brief asks to choose between: never a benchmark, a
    leaderboard, an article or a website, and never one the brief rules out. Name each the way the
    sources do, shortly.
    """

    brief: str = dspy.InputField()
    background: str = dspy.InputField(desc="search results on the topic")
    count: int = dspy.InputField(desc="the most options to return")
    entities: list[str] = dspy.OutputField()


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


class Assess(dspy.Signature):
    """State what the evidence says about one option on one criterion.

    Use only claims about this option. The finding is a table cell: at most six words, without the
    option's name, such as "24 GB at 4-bit" or "Apache 2.0". Return an empty finding and no claim
    ids if no claim speaks to it.
    """

    brief: str = dspy.InputField()
    entity: str = dspy.InputField(desc="the option")
    criterion: str = dspy.InputField()
    evidence: str = dspy.InputField(desc="one claim per line, prefixed by its id in brackets")
    finding: str = dspy.OutputField(desc="at most six words, or empty")
    claims: list[int] = dspy.OutputField(desc="ids of the claims the finding rests on")


class RankCriterion(dspy.Signature):
    """Rank options on one criterion, best first, from what the evidence says about each."""

    brief: str = dspy.InputField()
    criterion: str = dspy.InputField()
    options: str = dspy.InputField(desc="one option per line, with its finding")
    ranking: list[str] = dspy.OutputField(desc="every option's name, best first")


class Outline(dspy.Signature):
    """Outline a research report answering a brief from research notes, in the shape asked for."""

    brief: str = dspy.InputField()
    shape: str = dspy.InputField(desc="how the report is organised")
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
