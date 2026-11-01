"""What the routes take, and the answers of the calls that are not runs."""

from __future__ import annotations

from pydantic import BaseModel, Field

from kvasir.schemas.results import Output


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


class SessionRequest(BaseModel):
    """Body of `POST /v1/session`. The id is the caller's, so Open WebUI can key it by chat id."""

    session_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    model_fast: str | None = None
    model_strong: str | None = None


class StepRequest(BaseModel):
    """Body of `POST /v1/session/{id}/step`. An empty utterance advances the round table."""

    utterance: str = ""
