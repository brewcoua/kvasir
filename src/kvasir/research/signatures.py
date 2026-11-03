"""The signatures more than one research method uses. Each call sees one small input."""

from __future__ import annotations

from typing import Literal

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, field_validator

from kvasir.research.claims import Confidence

Support = Literal["supported", "partial", "unsupported"]


class Finding(BaseModel):
    claim: str
    quote: str
    confidence: Confidence

    @field_validator("confidence", mode="before")
    @classmethod
    def _moderate(cls, value: object) -> object:
        # Models reach for "moderate" often enough that it fails whole replies.
        return "medium" if value == "moderate" else value


class Intake(dspy.Signature):
    """Extract the facts in a piece of a web page that help answer a research brief.

    The page was found while searching for the question, so look for its answer first, then for
    anything else the brief needs. Each claim is one self-contained fact. Its quote is copied
    exactly from the text, word for word, short but enough to support the claim. Return an empty
    list if nothing in the text helps.
    """

    brief: str = dspy.InputField()
    question: str = dspy.InputField(desc="what the search that found the page was for")
    title: str = dspy.InputField()
    text: str = dspy.InputField(desc="a piece of the page")
    findings: list[Finding] = dspy.OutputField()


class WriteSection(dspy.Signature):
    """Write one section of a research report from its evidence.

    Use only the evidence. Cite every factual sentence with the bracketed ids of the claims that
    support it, such as [3] or [3][7]. Say plainly what the evidence does not settle. Do not repeat
    what earlier sections said. Do not write the section title. When subsections are given, write
    each under its own `###` heading, in that order, and add no other heading.

    Style:
    - Start with the finding, not with what the section will cover. Name who did what, in the
      active voice.
    - Prefer the evidence's numbers, dates and names to general words.
    - One idea per sentence, and sentences of different lengths.
    - No filler: no "it is important to note", "notably", "overall", "in conclusion", no
      rhetorical questions, no "not X but Y" setups, no lists of three for rhythm, no em dashes.
    - No inflated words such as "crucial", "pivotal", "landscape", "delve", "multifaceted",
      "testament", "tapestry".
    - Bold the one or two terms a reader scanning the paragraph should catch, with **, no more.
    - At most one callout, and only for a point that needs one: a line `> [!WARNING]` for evidence
      that is disputed or contradicted, `> [!CAUTION]` for a likely misreading, `> [!IMPORTANT]`
      for the point the section turns on, or `> [!NOTE]` for context, then its cited sentences on
      lines starting with `> `.
    """

    brief: str = dspy.InputField()
    outline: str = dspy.InputField()
    section: str = dspy.InputField()
    subsections: str = dspy.InputField(desc="the `###` headings to use, one per line, or empty")
    earlier_sections: str = dspy.InputField(desc="the start of each section already written")
    evidence: str = dspy.InputField(desc="one claim per line, prefixed by its id in brackets")
    text: str = dspy.OutputField(desc="the section's paragraphs, in markdown")


class Summarize(dspy.Signature):
    """Sum up a research report for a reader who reads nothing else.

    Each point is one plain sentence giving a finding, most important first, and keeps the
    bracketed claim ids the report cites for it. The first point answers the brief. Add nothing the
    report does not say. No filler, no em dashes.
    """

    brief: str = dspy.InputField()
    report: str = dspy.InputField(desc="the start of each section, with its claim ids")
    points: list[str] = dspy.OutputField(desc="three to five points, each citing claim ids")


class Verify(dspy.Signature):
    """Decide whether a sentence from a report is supported by the evidence it cites.

    Supported means every fact in the sentence is stated in the quotes, or follows directly from
    them. Partial means some facts are stated and others are added: a number, a name or a
    comparison the quotes do not contain. Unsupported means the quotes do not state what the
    sentence says. Wording may differ.
    """

    sentence: str = dspy.InputField()
    evidence: str = dspy.InputField(desc="each cited claim, with the quote it was read from")
    verdict: Support = dspy.OutputField()
    reason: str = dspy.OutputField(desc="one short sentence")


class Escalate(dspy.Signature):
    """Decide whether a sentence from a report is supported by the evidence it cites, and when it
    is only partly, rewrite it.

    Supported means every fact in the sentence is stated in the quotes, or follows directly from
    them. Partial means some facts are stated and others are added: a number, a name or a
    comparison the quotes do not contain. Unsupported means the quotes do not state what the
    sentence says. Wording may differ. For a partial sentence, rewrite it to state only what the
    quotes say, in the same style, keeping only the claim markers it already cites.
    """

    sentence: str = dspy.InputField()
    evidence: str = dspy.InputField(desc="each cited claim, with the quote it was read from")
    verdict: Support = dspy.OutputField()
    reason: str = dspy.OutputField(desc="one short sentence")
    rewrite: str = dspy.OutputField(desc="the rewritten sentence if partial, otherwise empty")


class Contradicts(dspy.Signature):
    """Decide whether two claims from different sources disagree about the same fact.

    They disagree when both cannot be true at once, such as two different dates for one event.
    Different facts about the same subject do not disagree, and neither does one being vaguer.
    """

    first: str = dspy.InputField(desc="a claim, with the quote it was read from")
    second: str = dspy.InputField(desc="a claim, with the quote it was read from")
    disagree: bool = dspy.OutputField()
