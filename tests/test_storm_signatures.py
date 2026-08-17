"""The fork's signatures must survive the adapter that now turns them into prompts.

dspy builds the prompt from field names, types and descriptions, and parses the answer back by the
same contract, so a signature the adapter cannot format or a typed field it cannot parse is a
runtime failure in the middle of a run rather than an import error. These cover both ends without a
gateway: formatting every signature the fork declares, and parsing back the ones whose output is no
longer plain text.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any

import dspy
import pytest
from dspy.utils.dummies import DummyLM

import kvasir.storm
import kvasir.storm.interface  # noqa: F401  imported first: the fork's modules import circularly
from kvasir.storm.collaborative_storm.modules.costorm_expert_utterance_generator import (
    GenExpertActionPlanning,
)
from kvasir.storm.collaborative_storm.modules.expert_generation import GenerateExpertGeneral
from kvasir.storm.collaborative_storm.modules.information_insertion_module import (
    ExpandSection,
    InsertInformation,
    InsertInformationCandidateChoice,
)
from kvasir.storm.storm_wiki.modules.knowledge_curation import QuestionToQuery
from kvasir.storm.storm_wiki.modules.persona_generator import FindRelatedTopic, GenPersona


def _signatures() -> list[type[dspy.Signature]]:
    found: dict[str, type[dspy.Signature]] = {}
    for module in pkgutil.walk_packages(kvasir.storm.__path__, "kvasir.storm."):
        imported = importlib.import_module(module.name)
        for _, obj in vars(imported).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, dspy.Signature)
                and obj is not dspy.Signature
            ):
                found[f"{obj.__module__}.{obj.__name__}"] = obj
    return list(found.values())


@pytest.mark.parametrize("signature", _signatures(), ids=lambda s: s.__name__)
def test_every_signature_formats_into_a_prompt(signature: type[dspy.Signature]) -> None:
    messages = dspy.ChatAdapter().format(signature, [], dict.fromkeys(signature.input_fields, "x"))

    assert messages[0]["role"] == "system"


@pytest.mark.parametrize(
    "signature, inputs, answer, field, expected",
    [
        (
            GenPersona,
            {"topic": "t", "examples": "e"},
            {"personas": ["editor one: a", "editor two: b"]},
            "personas",
            ["editor one: a", "editor two: b"],
        ),
        (
            FindRelatedTopic,
            {"topic": "t"},
            {"reasoning": "r", "related_topics": ["https://example.org/a"]},
            "related_topics",
            ["https://example.org/a"],
        ),
        (
            GenerateExpertGeneral,
            {"topic": "t", "background_info": "b", "topN": "2"},
            {"experts": ["role: description"]},
            "experts",
            ["role: description"],
        ),
        (
            QuestionToQuery,
            {"topic": "t", "question": "q"},
            {"queries": ["first query", "second query"]},
            "queries",
            ["first query", "second query"],
        ),
        (
            ExpandSection,
            {"section": "s", "info": "i"},
            {"subsections": ["one", "two"]},
            "subsections",
            ["one", "two"],
        ),
        # Upstream picked these out of prose with substring scans and regexes, and raised
        # "Undefined" when the model phrased its answer differently.
        (
            InsertInformation,
            {"intent": "i", "structure": "s"},
            {"reasoning": "r", "action": "step", "node_name": "node2"},
            "action",
            "step",
        ),
        (
            InsertInformationCandidateChoice,
            {"intent": "i", "choices": "1: a"},
            {"best_placement": 2},
            "best_placement",
            2,
        ),
        (
            InsertInformationCandidateChoice,
            {"intent": "i", "choices": "1: a"},
            {"best_placement": None},
            "best_placement",
            None,
        ),
        (
            GenExpertActionPlanning,
            {"topic": "t", "expert": "e", "summary": "s", "last_utterance": "l"},
            {"action_type": "Potential Answer", "description": "d"},
            "action_type",
            "Potential Answer",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_typed_output_is_parsed_back_into_its_type(
    signature: type[dspy.Signature],
    inputs: dict[str, str],
    answer: dict[str, Any],
    field: str,
    expected: Any,
) -> None:
    predictor = dspy.ChainOfThought(signature) if "reasoning" in answer else dspy.Predict(signature)

    with dspy.context(lm=DummyLM([answer])):
        prediction = predictor(**inputs)

    assert getattr(prediction, field) == expected
