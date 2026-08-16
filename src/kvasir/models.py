"""Request and response schemas for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    """Body of `POST /v1/research`.

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


class Citation(BaseModel):
    """One source, numbered as the article's `[n]` markers reference it."""

    index: int
    url: str
    title: str
    snippet: str


class ResearchResult(BaseModel):
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


class Error(BaseModel):
    """Payload of an `error` event."""

    message: str
