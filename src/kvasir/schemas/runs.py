"""A run as the registry serves it: its state, progress, model calls and usage."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from kvasir.schemas.results import RunResult


class Progress(BaseModel):
    """Payload of a `progress` event."""

    stage: str
    detail: str
    # Unix time the event was published, so a client can place it on a timeline.
    at: float = Field(default_factory=time.time)
    # How far through the stage the run is, when the stage has a countable unit such as sections.
    # `total` is an upper bound for research turns, since a conversation may end early.
    done: int | None = None
    total: int | None = None
    # Its place in the run, numbered from 0, so a model call can name the event it followed. Null
    # outside a run, and in runs stored before events were numbered.
    seq: int | None = None


class Error(BaseModel):
    """Payload of an `error` event."""

    message: str


class ModelUsage(BaseModel):
    """What one model, or one role, spent on a run."""

    calls: int
    prompt_tokens: int
    completion_tokens: int
    # Part of `completion_tokens`, not in addition to it. Zero when the gateway does not report it.
    reasoning_tokens: int
    # Whatever the gateway reported per call. Zero on a cache hit, and zero for a gateway that
    # reports no cost at all, which is not the same as free.
    cost: float
    # Wall time of the calls that answered, summed, so output tokens over it is a rate. It includes
    # reading the prompt, since nothing streams. Defaulted, so a run stored before it loads.
    seconds: float = 0.0


class StageUsage(ModelUsage):
    """What one stage spent, including what is not a completion."""

    embedding_tokens: int
    searches: int


class RunUsage(BaseModel):
    """A run's totals, and the breakdowns worth having."""

    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost: float
    embedding_tokens: int
    searches: int
    by_model: dict[str, ModelUsage]
    by_role: dict[str, ModelUsage]
    # Keyed by the stage the run was in when the call was made.
    by_stage: dict[str, StageUsage]


class LmCall(BaseModel):
    """One completion request, while it is waiting and after it returns."""

    # Numbered from 0 within the run. Null in runs stored before calls were numbered.
    id: int | None = None
    # The `seq` of the last event published in the work that made the call. Null when the call came
    # before any event, or in runs stored before calls were linked.
    event: int | None = None
    # Whether its messages and response were kept, for `GET /v1/runs/{id}/calls/{call}`.
    has_content: bool = False
    role: str | None
    model: str
    started_at: float
    # Null while the call is in flight.
    ended_at: float | None
    # Null while in flight, and when the call failed.
    prompt_tokens: int | None
    completion_tokens: int | None
    failed: bool


class LmCallContent(BaseModel):
    """What one completion was sent and what it answered, kept with KVASIR_KEEP_CALL_CONTENT."""

    # As sent to the gateway, in the OpenAI chat shape.
    messages: list[dict[str, Any]]
    # Null when the call failed.
    response: str | None
    # A reasoning model's thinking, when the gateway returns it separately.
    reasoning: str | None = None


class RunSummary(BaseModel):
    """One run, as `GET /v1/runs` lists it."""

    id: str
    kind: str
    topic: str
    state: str
    stage: str | None
    created_at: float
    started_at: float | None
    ended_at: float | None
    error: str | None
    usage: RunUsage
    # The stages a run of this kind goes through, in order, when that is known up front.
    planned_stages: list[str]
    latest: Progress | None
    # Whether the detail carries a result, so a list can offer it without fetching every detail.
    has_result: bool


class RunSpan(BaseModel):
    """One stretch of time spent in a stage. `ended_at` is null while the run is still in it."""

    started_at: float
    ended_at: float | None


class RunStage(BaseModel):
    """Time spent in one stage.

    A stage can be entered more than once, as Co-STORM alternates between two, so `seconds` sums
    the spans rather than measuring from the first entry to the last exit.
    """

    name: str
    started_at: float
    ended_at: float | None
    seconds: float
    spans: list[RunSpan]


class RunStatus(RunSummary):
    """A run's live state, as the `status` event of `GET /v1/runs/{id}/events` carries it."""

    stages: list[RunStage]
    # The most recent completions, oldest first, including those still in flight.
    calls: list[LmCall]


class RunDetail(RunStatus):
    """One run, as `GET /v1/runs/{id}` returns it. Its `calls` are every call the run kept."""

    events: list[Progress]
    # What the run returned, once it has.
    result: RunResult | None


class RunStarted(BaseModel):
    """Payload of the `run` event, the first frame of every streaming response."""

    run_id: str
