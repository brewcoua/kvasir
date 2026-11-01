"""What the brief reads, and choosing what a ranking or a table compares."""

from __future__ import annotations

from kvasir.methods.ledger.signatures import Criteria, Entities, PlanQueries
from kvasir.research.calls import predict
from kvasir.research.search import search
from kvasir.research.text import names
from kvasir.runs.progress import BACKGROUND, ProgressStream
from kvasir.schemas.requests import Clarification
from kvasir.schemas.results import Criterion
from kvasir.storm.lm import GatewayModel
from kvasir.storm.rm import SearXNG

# Searches whose snippets the criteria and the options are chosen from.
BACKGROUND_QUERIES = 3


def candidates(
    models: dict[str, GatewayModel],
    searxng: SearXNG,
    today: str,
    brief: str,
    criteria: list[Criterion],
    entities: list[str],
    max_criteria: int,
    max_entities: int,
    per_query: int,
    stream: ProgressStream,
) -> tuple[list[Criterion], list[str]]:
    """The criteria and the options to compare, chosen from search snippets where not given."""
    if not (criteria and entities):
        stream.publish(BACKGROUND, "searching for the options and what sets them apart")
        queries = predict(
            models["plan"],
            PlanQueries,
            today=today,
            brief=brief,
            notes="",
            count=BACKGROUND_QUERIES,
        ).queries[:BACKGROUND_QUERIES]
        # Snippets only, and not marked seen: the research still reads these pages.
        background = "\n".join(
            f"{hit['title']}: {hit['description']}"
            for hit in search(searxng, queries, per_query, set())
        )
        if not criteria:
            stream.publish(BACKGROUND, "choosing the criteria")
            chosen = predict(
                models["criteria"], Criteria, brief=brief, background=background
            ).criteria
            criteria = [
                Criterion(name=c.name, weight=min(max(c.weight, 1), 3))
                for c in chosen[:max_criteria]
            ]
        if not entities:
            stream.publish(BACKGROUND, "choosing the options to compare")
            chosen = predict(
                models["entities"], Entities, brief=brief, background=background, count=max_entities
            ).entities
            # A small model fills a list with plausible names, such as model sizes never released;
            # searching for those finds nothing. Only options the results mention are kept.
            entities = [entity for entity in chosen if names(entity, background)][:max_entities]
    # The first spelling of a name given twice wins.
    keys = [_key(c.name) for c in criteria]
    criteria = [c for i, c in enumerate(criteria) if keys[i] and keys[i] not in keys[:i]]
    keys = [_key(e) for e in entities]
    entities = [e.strip() for i, e in enumerate(entities) if keys[i] and keys[i] not in keys[:i]]
    stream.publish(
        BACKGROUND,
        f"comparing {len(entities)} options on {len(criteria)} criteria: {', '.join(entities)}",
    )
    return criteria, entities


def clarifications(clarifications: list[Clarification]) -> str:
    if not clarifications:
        return "none"
    return "\n".join(f"Q: {c.question}\nA: {c.answer}" for c in clarifications)


def _key(name: str) -> str:
    return name.strip().lower()
