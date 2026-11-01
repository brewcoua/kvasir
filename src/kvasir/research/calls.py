"""Calling a model with a signature."""

from __future__ import annotations

from typing import Any

import dspy  # type: ignore[import-untyped]

from kvasir.storm.lm import GatewayModel


def predict(model: GatewayModel, signature: type[dspy.Signature], **inputs: Any) -> Any:
    with dspy.context(lm=model):
        return dspy.Predict(signature)(**inputs)
