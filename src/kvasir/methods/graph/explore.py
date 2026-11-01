"""Exploring the graph: search for the question worth most, take in its claims, ask what they raise.

Each step reads only the question's path from its first question and the evidence found for it,
never the whole graph, so its input stays small however large the graph grows. A question is not
judged again on what other branches find: that would read the whole graph.
"""

from __future__ import annotations

import logging

from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.methods.graph.graph import path
from kvasir.methods.graph.signatures import Expand
from kvasir.methods.ledger.frontier import BRANCH_STALL, Frontier, roots
from kvasir.methods.ledger.loop import FAILED_SEARCHES, QUERY_SHAPES
from kvasir.methods.ledger.questions import Question, add_questions, as_results
from kvasir.methods.ledger.signatures import SearchFor
from kvasir.research.calls import predict
from kvasir.research.claims import Claim, quoted_claims
from kvasir.research.read import read
from kvasir.research.search import SearchUnavailable, search
from kvasir.runs.progress import RESEARCH, ProgressStream
from kvasir.runs.run import record_questions
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel
from kvasir.storm.rm import SearXNG

# Questions one step raises at most.
RAISED = 2

logger = logging.getLogger(__name__)


def explore(
    models: dict[str, GatewayModel],
    encoder: Encoder,
    searxng: SearXNG,
    today: str,
    brief: str,
    questions: list[Question],
    claims: list[Claim],
    frontier: Frontier,
    budget: int,
    per_query: int,
    max_attempts: int,
    max_depth: int,
    branch_searches: int,
    stream: ProgressStream,
) -> int:
    """Explore from `questions` best first, and return how many searches that took.

    Stops when `budget` searches are spent or every branch is closed. A branch closes when
    `BRANCH_STALL` searches in a row on it add no claim and no source, or once it has spent
    `branch_searches`. A question raises others only above `max_depth`.
    """
    seen: set[str] = set()
    spent: dict[int, int] = {}
    searches = failed = 0
    failures: list[str] = []
    while searches < budget:
        question = frontier.pick(questions, max_attempts)
        if question is None:
            break
        searches += 1
        stream.publish(RESEARCH, f"search {searches} of at most {budget}, for: {question.text}")
        query = str(
            predict(
                models["plan"],
                SearchFor,
                today=today,
                brief=brief,
                question=question.text,
                tried="\n".join(question.queries) or "none",
                missing=question.missing,
                shape=QUERY_SHAPES[min(question.attempts, len(QUERY_SHAPES) - 1)],
            ).query
        ).strip()
        try:
            hits = search(searxng, [query], per_query, seen)
        except SearchUnavailable as exc:
            failed += 1
            failures += exc.failures
            if failed == FAILED_SEARCHES:
                raise SearchUnavailable(failed, failures) from None
            continue
        failed = 0
        failures = []
        question.queries.append(query)
        new = read(
            models["intake"],
            encoder,
            brief,
            question.text,
            hits,
            len(claims) + 1,
            searches,
            f"search {searches}",
            stream,
            models.get("ground"),
        )
        claims.extend(new)
        root = roots(questions)[question.id]
        spent[root] = spent.get(root, 0) + 1
        if frontier.searched(questions, question, new):
            stream.publish(
                RESEARCH,
                f"closing the branch of {question.text}: "
                f"{BRANCH_STALL} searches in a row added nothing new",
            )
        elif spent[root] >= branch_searches and root not in frontier.closed:
            frontier.closed.add(root)
            stream.publish(
                RESEARCH,
                f"closing the branch of {question.text}: it spent its {branch_searches} searches",
            )
        if new:
            raised = expand(models["check"], brief, questions, claims, question, new)
            if question.depth < max_depth:
                add_questions(encoder, questions, raised[:RAISED], question)
        if question.status == "open" and question.attempts >= max_attempts:
            question.status = "unresolved"
        # Before the event, so the status that follows it carries the graph as it now stands.
        record_questions(as_results(questions))
        stream.publish(
            RESEARCH,
            f"search {searches}: {len(new)} new claims, {len(claims)} in all; "
            f"{question.text}: {question.status}",
        )
    return searches


def expand(
    model: GatewayModel,
    brief: str,
    questions: list[Question],
    claims: list[Claim],
    question: Question,
    new: list[Claim],
) -> list[str]:
    """Judge `question` on the claims it already rests on and those its search just found, and
    return the questions they raise."""
    held = {c.id: c for c in claims if c.id in question.claims}
    shown = list(held.values()) + new
    above = path(questions, question)
    try:
        judged = predict(
            model,
            Expand,
            brief=brief,
            path="\n".join(q.text for q in above) or "none",
            question=question.text,
            evidence=quoted_claims(shown),
        )
    except AdapterParseError as exc:
        logger.warning("could not read the judgement on %r: %s", question.text, exc)
        return []
    question.judged_at = len(claims)
    # Only ids it was shown: a small model otherwise cites claims from its own numbering.
    ids = [c.id for c in shown if c.id in judged.claims]
    if judged.status == "open" or not ids:
        question.status, question.claims = "open", []
    else:
        question.status, question.claims = judged.status, ids
    question.missing = "" if question.status == "answered" else str(judged.missing).strip()
    return [str(q) for q in judged.raised]
