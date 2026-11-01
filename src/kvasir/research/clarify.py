"""The questions worth asking before a run starts. One fast-model call, outside any run."""

from __future__ import annotations

import dspy  # type: ignore[import-untyped]

from kvasir.config import Settings
from kvasir.runners import build_research_models


class Clarify(dspy.Signature):
    """Before researching a question, decide what you would need to ask its author.

    Ask only what would change what you research or how you report it, such as scope, region,
    period, or what to compare. Ask at most three questions. Return none if the question is clear.
    """

    question: str = dspy.InputField()
    questions: list[str] = dspy.OutputField(desc="at most three short questions, or an empty list")


def clarify(settings: Settings, question: str) -> list[str]:
    """Questions worth asking before researching `question`, or none."""
    model = build_research_models(settings)["clarify"]
    with dspy.context(lm=model):
        return list(dspy.Predict(Clarify)(question=question).questions[:3])
