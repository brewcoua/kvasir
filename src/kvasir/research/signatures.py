"""The signatures more than one research method uses. Each call sees one small input."""

from __future__ import annotations

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel

from kvasir.research.claims import Confidence


class Finding(BaseModel):
    claim: str
    quote: str
    confidence: Confidence


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


class Contradicts(dspy.Signature):
    """Decide whether two claims from different sources disagree about the same fact.

    They disagree when both cannot be true at once, such as two different dates for one event.
    Different facts about the same subject do not disagree, and neither does one being vaguer.
    """

    first: str = dspy.InputField(desc="a claim, with the quote it was read from")
    second: str = dspy.InputField(desc="a claim, with the quote it was read from")
    disagree: bool = dspy.OutputField()
