"""What a run produces, as its `done` event and its detail carry it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Tier = Literal["high", "normal", "unknown", "low", "avoid"]


class SourceInfo(BaseModel):
    """What is known of a site: its kind, tags and trust tier. See `kvasir.research.sources`."""

    kind: str
    tags: list[str]
    tier: Tier
    # Empty when Wikidata gave none.
    label: str
    # The Wikidata item, or empty.
    qid: str
    # What decided it: config, mark, list, rule or wikidata. Empty when nothing is known. A site
    # set in the config cannot be marked.
    origin: str


class Citation(BaseModel):
    """One source, numbered as the article's `[n]` markers reference it."""

    index: int
    url: str
    title: str
    snippet: str
    # Null when `[sources] enabled` is false, and in runs stored before it existed.
    source: SourceInfo | None = None


class ArticleResult(BaseModel):
    """Payload of the `done` event."""

    article: str
    outline: str
    citations: list[Citation]
    duration_seconds: float


Output = Literal["report", "ranking", "table"]


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
    # Read from the search snippet because the page could not be fetched. Defaulted, so a stored
    # run still loads.
    from_snippet: bool = False
    # Claims from other sites that support this one, and that disagree with it. Filled only when
    # the run was asked to corroborate, or to look for contradictions.
    corroborated_by: list[int] = []
    contradicted_by: list[int] = []


class ResearchQuestion(BaseModel):
    """One question the research searched for, and how far it got."""

    id: int
    text: str
    # A few words naming it, or empty when the model gave none or the run predates titles.
    title: str = ""
    # The question this one follows up on.
    parent: int | None
    depth: int
    status: Literal["open", "weak", "answered", "contradicted", "unresolved"]
    # The claims whose quotes support the answer.
    claims: list[int]
    queries: list[str]
    # What the last judgement found missing.
    missing: str


# The step of verification that decided a sentence. See `kvasir.research.verify`.
Stage = Literal["citation", "checker", "verify", "escalate", "rewrite"]


class RemovedSentence(BaseModel):
    """A sentence verification took out of the report, and why."""

    section: str
    # As written, so its `[n]` markers are claim ids rather than source numbers.
    sentence: str
    reason: str
    # Defaulted, so a run stored before verification had steps still loads: it had only one.
    stage: Stage = "verify"


class RevisedSentence(BaseModel):
    """A sentence verification found partly unsupported and rewrote to what its quotes say."""

    section: str
    # Both as written, with claim ids.
    before: str
    after: str
    reason: str


class UnverifiedSentence(BaseModel):
    """A sentence no step could judge, left in the report marked `[unverified]`."""

    section: str
    sentence: str
    reason: str


class LowTrustSentence(BaseModel):
    """A sentence whose cited claims all come from sites of low trust, left in the report."""

    section: str
    sentence: str


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
    revised: list[RevisedSentence] = []
    unverified: list[UnverifiedSentence] = []
    low_trust: list[LowTrustSentence] = []
    output: Output = "report"
    criteria: list[Criterion] = []
    # Best first for a ranking. Empty for a report.
    matrix: list[Row] = []
    duration_seconds: float


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
