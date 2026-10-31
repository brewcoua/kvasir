"""Request and response schemas for the HTTP API."""

from __future__ import annotations

import time

from pydantic import BaseModel, Field


class ArticleRequest(BaseModel):
    """Body of `POST /v1/article`.

    Every field but the topic falls back to the configured default when omitted.
    """

    topic: str = Field(min_length=1)
    search_top_k: int | None = Field(default=None, ge=1)
    max_conv_turn: int | None = Field(default=None, ge=1)
    max_perspective: int | None = Field(default=None, ge=1)
    do_polish_article: bool = True
    # Passed to the gateway verbatim. Not validated against any list of known models.
    model_fast: str | None = None
    model_strong: str | None = None


class TaskRequest(BaseModel):
    """Body of `POST /v1/tasks`. OpenAI-shaped chat messages, forwarded to the fast model as-is."""

    messages: list[dict[str, str]] = Field(min_length=1)


class TaskResponse(BaseModel):
    """Payload of `POST /v1/tasks`. One completion, unparsed."""

    content: str


class Citation(BaseModel):
    """One source, numbered as the article's `[n]` markers reference it."""

    index: int
    url: str
    title: str
    snippet: str


class ArticleResult(BaseModel):
    """Payload of the `done` event."""

    article: str
    outline: str
    citations: list[Citation]
    duration_seconds: float


class SessionRequest(BaseModel):
    """Body of `POST /v1/session`. The id is the caller's, so Open WebUI can key it by chat id."""

    session_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    model_fast: str | None = None
    model_strong: str | None = None


class StepRequest(BaseModel):
    """Body of `POST /v1/session/{id}/step`. An empty utterance advances the round table."""

    utterance: str = ""


class Turn(BaseModel):
    """One conversation turn, as the `done` event of a step."""

    role: str
    role_description: str
    utterance: str
    utterance_type: str
    citations: list[Citation]
    mind_map_reorganised: bool


class Report(BaseModel):
    """Payload of `POST /v1/session/{id}/report`."""

    report: str
    citations: list[Citation]


class SessionInfo(BaseModel):
    """Payload of `GET /v1/session/{id}`."""

    session_id: str
    topic: str
    turn_count: int
    experts: list[str]
    updated_at: float


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


class RunDetail(RunStatus):
    """One run, as `GET /v1/runs/{id}` returns it."""

    events: list[Progress]
    # What the run returned, once it has.
    result: ArticleResult | Turn | Report | SessionInfo | None


class RunStarted(BaseModel):
    """Payload of the `run` event, the first frame of every streaming response."""

    run_id: str
