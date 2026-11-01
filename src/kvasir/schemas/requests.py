"""What the routes take, and the answers of the calls that are not runs."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kvasir.schemas.results import Output


class TaskRequest(BaseModel):
    """Body of `POST /v1/tasks`. OpenAI-shaped chat messages, forwarded to the fast model as-is."""

    messages: list[dict[str, str]] = Field(min_length=1)


class TaskResponse(BaseModel):
    """Payload of `POST /v1/tasks`. One completion, unparsed."""

    content: str


class Clarification(BaseModel):
    """One question `/v1/clarify` asked, and the user's answer to it."""

    question: str
    answer: str


class ClarifyRequest(BaseModel):
    """Body of `POST /v1/clarify`."""

    question: str = Field(min_length=1)


class ClarifyResponse(BaseModel):
    """Payload of `POST /v1/clarify`. Empty when the question needs no clarifying."""

    questions: list[str]


class Input(BaseModel):
    """What a run is about. For STORM, the question is the article's topic."""

    question: str = Field(min_length=1)
    # Only a method that clarifies reads them.
    clarifications: list[Clarification] = []


class LedgerOptions(BaseModel):
    """Options of the `ledger` method. The budgets fall back to the configured defaults."""

    model_config = ConfigDict(extra="forbid")

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
    # `breadth` searches the least tried question first. `worth` orders questions by a score
    # computed from the claims held, merges repeated claims, and closes a branch that stops adding.
    frontier: Literal["breadth", "worth"] = "breadth"
    # Check each claim, and each sentence citing one claim, with the grounding checker. On when
    # one is configured; without one, it does nothing.
    ground: bool | None = None
    # Compare each claim with the closest claims from other sites, for support and for
    # disagreement.
    corroborate: bool = False
    contradict: bool = False
    # Passed to the gateway verbatim. Not validated against any list of known models.
    model_fast: str | None = None
    model_strong: str | None = None


class GraphOptions(BaseModel):
    """Options of the `graph` method. The budgets fall back to the configured defaults."""

    model_config = ConfigDict(extra="forbid")

    max_searches: int | None = Field(default=None, ge=1)
    results_per_query: int | None = Field(default=None, ge=1)
    max_attempts: int | None = Field(default=None, ge=1)
    # Levels of raised questions below the first questions.
    max_depth: int | None = Field(default=None, ge=1)
    # Searches one branch may spend before it is closed, however much it still adds.
    branch_searches: int = Field(default=6, ge=1)
    ground: bool | None = None
    corroborate: bool = False
    contradict: bool = False
    model_fast: str | None = None
    model_strong: str | None = None


class StormOptions(BaseModel):
    """Options of the `storm` method. Each falls back to the configured default when omitted."""

    model_config = ConfigDict(extra="forbid")

    search_top_k: int | None = Field(default=None, ge=1)
    max_conv_turn: int | None = Field(default=None, ge=1)
    max_perspective: int | None = Field(default=None, ge=1)
    do_polish_article: bool = True
    model_fast: str | None = None
    model_strong: str | None = None


class LedgerRun(BaseModel):
    method: Literal["ledger"]
    input: Input
    options: LedgerOptions = LedgerOptions()


class GraphRun(BaseModel):
    method: Literal["graph"]
    input: Input
    options: GraphOptions = GraphOptions()


class StormRun(BaseModel):
    method: Literal["storm"]
    input: Input
    options: StormOptions = StormOptions()


# Body of `POST /v1/runs`. One member per entry of `kvasir.methods.METHODS`, so each method's
# options are typed in the schema and an unknown option is refused before the run starts.
RunRequest = Annotated[LedgerRun | GraphRun | StormRun, Field(discriminator="method")]


class MethodInfo(BaseModel):
    """One entry of `GET /v1/methods`."""

    name: str
    # Whether a client should ask `/v1/clarify` before starting it.
    clarifies: bool
    # The stages a run with the default options goes through.
    stages: list[str]
    # The options' JSON Schema, with the configured defaults as each property's `default`.
    options: dict[str, Any]


class SessionRequest(BaseModel):
    """Body of `POST /v1/session`. The id is the caller's, so Open WebUI can key it by chat id."""

    session_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    model_fast: str | None = None
    model_strong: str | None = None


class StepRequest(BaseModel):
    """Body of `POST /v1/session/{id}/step`. An empty utterance advances the round table."""

    utterance: str = ""
