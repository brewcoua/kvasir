"""The signatures only the graph method uses."""

from __future__ import annotations

from typing import Literal

import dspy  # type: ignore[import-untyped]

from kvasir.methods.ledger.signatures import NamedQuestion


class Expand(dspy.Signature):
    """Decide how well the evidence answers one research question, and what it raises next.

    The path is the chain of questions this one was asked under, from the first. Count a claim
    only if its quote states what the claim says. answered: the counted claims settle the
    question. weak: they settle part of it. contradicted: they disagree with each other. open: no
    claim speaks to it. Then ask the questions the evidence itself raises: something a claim
    names without explaining, a figure it gives without a source or a date, a point two claims
    disagree on. Ask none the path already asks.
    """

    brief: str = dspy.InputField()
    path: str = dspy.InputField(desc="the questions above this one, first first, or none")
    question: str = dspy.InputField()
    evidence: str = dspy.InputField(desc="each claim with the quote it was read from")
    status: Literal["answered", "weak", "contradicted", "open"] = dspy.OutputField()
    claims: list[int] = dspy.OutputField(desc="ids of the claims counted as support")
    missing: str = dspy.OutputField(desc="what is still missing, in a few words, or empty")
    raised: list[NamedQuestion] = dspy.OutputField(
        desc="at most two questions the evidence raises, or empty"
    )
