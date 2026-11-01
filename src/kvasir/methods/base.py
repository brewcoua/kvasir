"""What a research method is, as the run route and `GET /v1/methods` see it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from kvasir.config import Settings
from kvasir.runs.progress import ProgressStream
from kvasir.schemas.requests import Input
from kvasir.schemas.results import RunResult

OptionsT = TypeVar("OptionsT", bound=BaseModel)


@dataclass(frozen=True)
class Method(Generic[OptionsT]):
    name: str
    Options: type[OptionsT]
    # Whether a client should ask `/v1/clarify` before starting a run.
    clarifies: bool
    # The stages a run with these options plans to go through, for the runs page to show ahead.
    stages: Callable[[OptionsT], list[str]]
    # What each option left out resolves to, as configured.
    defaults: Callable[[Settings], dict[str, Any]]
    # Blocks for minutes. Never call it on the event loop.
    run: Callable[[Settings, Input, OptionsT, ProgressStream], RunResult]

    def schema(self, settings: Settings) -> dict[str, Any]:
        """The options' JSON Schema, with the configured defaults filled in."""
        schema = self.Options.model_json_schema()
        for name, value in self.defaults(settings).items():
            schema["properties"][name]["default"] = value
        return schema
