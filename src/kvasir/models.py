"""Request and response schemas for the HTTP API."""

from __future__ import annotations

import time
from typing import Any, Literal

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


class Clarification(BaseModel):
    """One question `/v1/research/clarify` asked, and the user's answer to it."""

    question: str
    answer: str


class ClarifyRequest(BaseModel):
    """Body of `POST /v1/research/clarify`."""

    question: str = Field(min_length=1)


class ClarifyResponse(BaseModel):
    """Payload of `POST /v1/research/clarify`. Empty when the question needs no clarifying."""

    questions: list[str]


Output = Literal["report", "ranking", "table"]


class ResearchRequest(BaseModel):
    """Body of `POST /v1/research`.

    The budgets fall back to the configured defaults when omitted.
    """

    question: str = Field(min_length=1)
    clarifications: list[Clarification] = []
    # Inferred from the question when omitted.
    output: Output | None = None
    # For a ranking or a table, chosen from a first search when omitted.
    criteria: list[str] | None = None
    entities: list[str] | None = None
    max_searches: int | None = Field(default=None, ge=1)
    results_per_query: int | None = Field(default=None, ge=1)
    # Query shapes tried on one question before giving it up.
    max_attempts: int | None = Field(default=None, ge=1)
    # Whether an answered question may add harder ones, and how many levels deep.
    follow_ups: bool | None = None
    max_depth: int | None = Field(default=None, ge=1)
    # For a ranking or a table, when the run chooses them.
    max_entities: int | None = Field(default=None, ge=1)
    max_criteria: int | None = Field(default=None, ge=1)
    model_fast: str | None = None
    model_strong: str | None = None


class Defaults(BaseModel):
    """Payload of `GET /v1/defaults`: what each optional run field takes when a request omits it.

    Keyed by the request field's name, research's and article's together, for a client to show.
    """

    model_fast: str
    model_strong: str
    max_searches: int
    results_per_query: int
    max_attempts: int
    follow_ups: bool
    max_depth: int
    max_entities: int
    max_criteria: int
    search_top_k: int
    max_conv_turn: int
    max_perspective: int
    do_polish_article: bool


class ResearchClaim(BaseModel):
    """One piece of evidence, with the verbatim quote it was read from."""

    id: int
    text: str
    quote: str
    url: str
    title: str
    confidence: Literal["high", "medium", "low"]
    # The search that found it. Defaulted, so a run stored when claims had rounds still loads.
    step: int = 0


class ResearchQuestion(BaseModel):
    """One question the research searched for, and how far it got."""

    id: int
    text: str
    # The question this one follows up on.
    parent: int | None
    depth: int
    status: Literal["open", "weak", "answered", "contradicted", "unresolved"]
    # The claims whose quotes support the answer.
    claims: list[int]
    queries: list[str]
    # What the last judgement found missing.
    missing: str


class RemovedSentence(BaseModel):
    """A sentence verification took out of the report, and why."""

    section: str
    # As written, so its `[n]` markers are claim ids rather than source numbers.
    sentence: str
    reason: str


class Criterion(BaseModel):
    """What a ranking or a table compares on, and how much it counts in the overall rank."""

    name: str
    weight: int = Field(default=1, ge=1, le=3)


class Cell(BaseModel):
    """What the evidence says about one entity on one criterion."""

    criterion: str
    # Empty when no claim spoke to it.
    finding: str
    claims: list[int]
    # This entity's place on this criterion, averaged over both ranking passes. Rankings only.
    rank: float | None = None


class Row(BaseModel):
    """One entity of a ranking or a table."""

    entity: str
    # Overall place, from the weighted mean of its criterion ranks. Rankings only.
    rank: int | None = None
    cells: list[Cell]


class ResearchResult(BaseModel):
    """Payload of the `done` event of a research run."""

    report: str
    # What the brief had to decide that the question and the clarifications left open.
    assumptions: list[str]
    citations: list[Citation]
    # Every claim gathered, cited or not, so a reader can audit the report against its evidence.
    claims: list[ResearchClaim]
    # Every question searched for, in the order asked. Defaulted, so an older stored run loads.
    questions: list[ResearchQuestion] = []
    # Defaulted, so a run stored before verification existed still loads.
    removed: list[RemovedSentence] = []
    output: Output = "report"
    criteria: list[Criterion] = []
    # Best first for a ranking. Empty for a report.
    matrix: list[Row] = []
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


# What a run can produce, as its `done` event and its detail carry it.
RunResult = ArticleResult | ResearchResult | Turn | Report | SessionInfo


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
