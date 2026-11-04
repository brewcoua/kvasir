"""Exploring the graph: search for the question proof numbers pick, take in its claims, ask what
they raise.

Each step reads only the question's path from its first question and the evidence found for it,
never the whole graph, so its input stays small however large the graph grows. A question is not
judged again on what other branches find: that would read the whole graph. It is judged again on
what its own children find, once they are all answered.
"""

from __future__ import annotations

import logging
import math
import time

from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]
from pydantic import ValidationError

from kvasir.methods.graph.graph import path
from kvasir.methods.graph.proof import children, pick
from kvasir.methods.graph.signatures import Expand, Gaps
from kvasir.methods.ledger.frontier import BRANCH_STALL, Frontier
from kvasir.methods.ledger.loop import FAILED_SEARCHES, QUERY_SHAPES
from kvasir.methods.ledger.questions import Question, add_questions, as_results
from kvasir.methods.ledger.signatures import NamedQuestion, SearchFor
from kvasir.research.calls import predict
from kvasir.research.claims import Claim, next_id, quoted_claims
from kvasir.research.corpus import Corpus
from kvasir.research.read import read, recall
from kvasir.research.search import SearchUnavailable, search
from kvasir.runs.progress import RESEARCH, ProgressStream
from kvasir.runs.run import record_questions
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel
from kvasir.storm.rm import SearXNG

# Progressive widening (Couëtoux et al. 2011): a question not yet answered, searched n times, may
# hold at most ceil(WIDEN * n ** ALPHA) children, and never more than MAX_CHILDREN; one that keeps
# coming back weak earns more. An answered question holds at most FOLLOW_UPS, a harder question
# going deeper, so the research does not end once the first questions are answered. Untuned first
# guesses.
WIDEN = 2
ALPHA = 0.5
MAX_CHILDREN = 4
FOLLOW_UPS = 1
# New first questions one gap call adds at most, and dry gap calls in a row before the research
# ends. A gap call is made once no question can still be settled and time is left. It is dry when it
# adds no question, or the questions it added found fewer than `GAP_YIELD` claims a search: the
# research has drifted from the brief to questions the web says little about.
GAP_QUESTIONS = 3
GAP_STALL = 2
GAP_YIELD = 2
# Questions in the graph at most. The time budget bounds what is searched; this bounds the input
# of the writing and the size of the graph shown.
MAX_QUESTIONS = 80
# The share of the research time, at its end, in which no question raises others and no gap call
# is made: a question asked then would go unsearched. See `proof` for what is searched instead.
LATE_SHARE = 0.25
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
    corpus: Corpus,
    deadline: float,
    late_at: float,
    budget: int | None,
    per_query: int,
    max_attempts: int,
    max_depth: int,
    stream: ProgressStream,
) -> int:
    """Explore from `questions`, and return how many searches that took.

    Stops at `deadline`, a `time.monotonic()`, once `budget` searches are spent when there is one,
    or once `GAP_STALL` gap calls in a row are dry. A question raises others only above
    `max_depth`, and only before `late_at`, after which the research stops once no question can
    still be settled.
    """

    def take_in(new: list[Claim], question: Question, late: bool) -> None:
        """Judge `question` on `new`, add the questions that raises, and settle what it answers."""
        raised = expand(models["check"], brief, questions, claims, question, new)
        if question.status == "answered":
            room = FOLLOW_UPS
        else:
            room = min(MAX_CHILDREN, math.ceil(WIDEN * question.attempts**ALPHA))
        room -= len(children(questions)[question.id])
        if question.depth < max_depth and room > 0 and not late:
            add_questions(encoder, questions, raised[:room], question, MAX_QUESTIONS)
        settle(models["check"], brief, questions, claims, question)

    seen: set[str] = set()
    # The pieces the corpus held when each question was last recalled for.
    recalled: dict[int, int] = {}
    searches = failed = stalled = 0
    failures: list[str] = []
    # Searches and claims when the last gap call was made.
    gapped: tuple[int, int] | None = None
    while (now := time.monotonic()) < deadline and (budget is None or searches < budget):
        late = now >= late_at
        question = pick(questions, frontier, max_attempts, late)
        if question is None and late:
            stream.publish(RESEARCH, "stopping: no question left to settle, and too little time")
            break
        if question is None:
            if gapped is not None and (spent := searches - gapped[0]):
                found = len(claims) - gapped[1]
                stalled = stalled + 1 if found < GAP_YIELD * spent else 0
            if stalled >= GAP_STALL:
                stream.publish(
                    RESEARCH, f"stopping: the last {GAP_STALL} gaps found little or nothing new"
                )
                break
            gapped = (searches, len(claims))
            asked = len(questions)
            add_questions(
                encoder, questions, gaps(models["decompose"], brief, questions), None, MAX_QUESTIONS
            )
            if len(questions) == asked:
                stalled += 1
            record_questions(as_results(questions))
            stream.publish(
                RESEARCH,
                f"no question left to settle; asked what the report still lacks: "
                f"{len(questions) - asked} new questions",
            )
            continue
        # As in the ledger loop: pieces already downloaded may answer it, and cost no search.
        if recalled.get(question.id) != len(corpus.pieces):
            recalled[question.id] = len(corpus.pieces)
            held = recall(
                models["intake"],
                encoder,
                brief,
                question.text,
                corpus,
                next_id(claims),
                searches,
                f"recall for {question.text}",
                stream,
                models.get("ground_intake"),
                question_id=question.id,
                claims_held=len(claims),
            )
            claims.extend(held)
            if held:
                frontier.merge(held)
                take_in(held, question, late)
                record_questions(as_results(questions))
                if question.status == "answered":
                    continue
        searches += 1
        left = math.ceil((deadline - time.monotonic()) / 60)
        attempt = stream.publish(
            RESEARCH,
            f"search {searches}, {left} min left, for: {question.text}",
            searches,
            budget,
            kind="attempt",
            question=question.id,
        )
        # A site that keeps giving confirmed claims is asked for this question, no plan needed.
        query = corpus.site_query(question.id, question.text, claims) if question.attempts else None
        if query is None:
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
            hits = search(
                searxng,
                [query],
                per_query,
                seen,
                encoder,
                question.text,
                frontier.held(),
                corpus=corpus,
                claims=claims,
                sources=corpus.sources,
            )
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
            next_id(claims),
            searches,
            f"search {searches}",
            stream,
            corpus,
            models.get("ground_intake"),
            attempt,
        )
        claims.extend(new)
        if frontier.searched(questions, question, new):
            stream.publish(
                RESEARCH,
                f"closing the branch of {question.text}: "
                f"{BRANCH_STALL} searches in a row added nothing new",
            )
        if new:
            take_in(new, question, late)
        if question.status == "open" and question.attempts >= max_attempts:
            question.status = "unresolved"
        # Before the event, so the status that follows it carries the graph as it now stands.
        record_questions(as_results(questions))
        # done and total carry the claim counts here, not progress through the stage.
        stream.publish(
            RESEARCH,
            f"search {searches}: {len(new)} new claims, {len(claims)} in all; "
            f"{question.text}: {question.status}",
            len(new),
            len(claims),
            kind="result",
            parent=attempt,
        )
    return searches


def gaps(model: GatewayModel, brief: str, questions: list[Question]) -> list[NamedQuestion]:
    """New first questions on what the questions asked so far leave out of the brief."""
    listed = "\n".join(f"{q.title or q.text}: {q.text} ({q.status})" for q in questions)
    try:
        return list(predict(model, Gaps, brief=brief, questions=listed).raised)[:GAP_QUESTIONS]
    except (AdapterParseError, ValidationError) as exc:
        logger.warning("could not read what the report still lacks: %s", exc)
        return []


def settle(
    model: GatewayModel,
    brief: str,
    questions: list[Question],
    claims: list[Claim],
    question: Question,
) -> None:
    """Once `question` is answered and so is every question beside it, judge their parent again
    on what they rest on, and so on up while each parent is answered in turn."""
    by_id = {c.id: c for c in claims}
    below = children(questions)
    above = {q.id: q for q in questions}
    while question.status == "answered" and question.parent is not None:
        parent = above[question.parent]
        if parent.status == "answered" or any(c.status != "answered" for c in below[parent.id]):
            return
        ids = sorted({i for c in below[parent.id] for i in c.claims} - set(parent.claims))
        # Raises nothing: a parent its children answered has nothing left to raise.
        expand(model, brief, questions, claims, parent, [by_id[i] for i in ids])
        question = parent


def expand(
    model: GatewayModel,
    brief: str,
    questions: list[Question],
    claims: list[Claim],
    question: Question,
    new: list[Claim],
) -> list[NamedQuestion]:
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
    except (AdapterParseError, ValidationError) as exc:
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
    return list(judged.raised)
