"""The question loop: search for one question at a time, judging each on what arrives."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]
from pydantic import ValidationError

from kvasir.methods.ledger.frontier import BRANCH_STALL, Frontier
from kvasir.methods.ledger.questions import Question, as_results
from kvasir.methods.ledger.signatures import CheckAnswer, SearchFor
from kvasir.research.calls import predict
from kvasir.research.claims import Claim, closest, quoted_claims
from kvasir.research.corpus import Corpus
from kvasir.research.read import read, recall
from kvasir.research.search import SearchUnavailable, search
from kvasir.runs.progress import RESEARCH, ProgressStream
from kvasir.runs.run import record_questions
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel
from kvasir.storm.rm import SearXNG

# The query shape for each attempt at a question, the last repeated: a question the plain words
# do not answer gets the words its answer would contain, then a primary source, then the broader
# topic. Whether `site:` and `filetype:` work depends on the engines SearXNG uses.
QUERY_SHAPES = (
    "general: the plain words someone would search for",
    "specific: the exact terms, names, figures or year the answer would contain",
    "a primary source: add site: and the official domain, or filetype:pdf for a report or paper",
    "a step back: the broader topic the answer sits in",
)
# Claims a question is judged on, the closest to it.
CLAIMS_PER_QUESTION = 8
# Searches in a row that add no claim before the research stops: one empty search is the cue to
# try another shape, not to stop, so this waits for several.
STALL_SEARCHES = 4
# Searches in a row that fail before the run stops, rather than write from no evidence.
FAILED_SEARCHES = 3

logger = logging.getLogger(__name__)

# Judges a question on the claims gathered so far, updating its status, claims and what it misses.
Judge = Callable[[Question], None]


class Research(Protocol):
    """Searches for questions within a budget of searches, judging each with the judge given.

    Its events are published under `stage`, its attempts under the event `parent`.
    """

    def __call__(
        self,
        questions: list[Question],
        budget: int,
        judge: Judge,
        stage: str = RESEARCH,
        parent: int | None = None,
    ) -> None: ...


def research(
    models: dict[str, GatewayModel],
    encoder: Encoder,
    searxng: SearXNG,
    today: str,
    brief: str,
    questions: list[Question],
    claims: list[Claim],
    seen: set[str],
    corpus: Corpus,
    budget: int,
    per_query: int,
    max_attempts: int,
    judge: Judge,
    first_step: int,
    stream: ProgressStream,
    frontier: Frontier | None = None,
    *,
    stage: str = RESEARCH,
    parent: int | None = None,
    ledger: list[Question] | None = None,
) -> int:
    """Search for `questions` one at a time, and return how many searches that took.

    Stops when `budget` searches are spent, no question is left to search for, or
    `STALL_SEARCHES` searches in a row find nothing. A question is judged again before it is
    searched for whenever claims arrived since its last judgement, since a search for one question
    often answers another. Adds what it finds to `claims`; `judge` may add to `questions`.

    With a `frontier`, questions are taken in the order of their worth rather than breadth first,
    and a branch that stops adding claims or sources is closed.

    Events are published under `stage`, and attempts and recalls under the event `parent`. A
    `ledger` is recorded on the run whenever a question in it changes; `questions` that are not
    part of one, as a repair's are not, leave it out.
    """

    def record() -> None:
        if ledger is not None:
            record_questions(as_results(ledger))

    searches = stalled = failed = 0
    # The pieces the corpus held when each question was last recalled for.
    recalled: dict[int, int] = {}
    failures: list[str] = []
    while searches < budget and stalled < STALL_SEARCHES:
        question = (
            frontier.pick(questions, max_attempts)
            if frontier is not None
            else pick(questions, max_attempts)
        )
        if question is None:
            break
        if question.judged_at < len(claims):
            judge(question)
            record()
            continue
        # Pieces already downloaded, by this run or an earlier one, may answer it, and cost no
        # search; recall marks what it reads, so it is asked again only once the corpus has grown.
        if recalled.get(question.id) != len(corpus.pieces):
            recalled[question.id] = len(corpus.pieces)
            held = recall(
                models["intake"],
                encoder,
                brief,
                question.text,
                corpus,
                len(claims) + 1,
                first_step + searches,
                f"recall for {question.text}",
                stream,
                models.get("ground_intake"),
                question_id=question.id,
                parent=parent,
                stage=stage,
            )
            claims.extend(held)
            if held:
                if frontier is not None:
                    frontier.merge(held)
                judge(question)
                record()
                if question.status == "answered":
                    continue
        searches += 1
        attempt = stream.publish(
            stage,
            f"search {searches} of at most {budget}, for: {question.text}",
            searches,
            budget,
            kind="attempt",
            parent=parent,
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
                frontier.held() if frontier is not None else None,
                corpus=corpus,
                claims=claims,
            )
        except SearchUnavailable as exc:
            # The attempt is not spent, since the query was never answered; the budget is.
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
            first_step + searches,
            f"search {searches}",
            stream,
            corpus,
            models.get("ground_intake"),
            attempt,
            stage,
        )
        claims.extend(new)
        if frontier is not None and frontier.searched(questions, question, new):
            stream.publish(
                stage,
                f"closing the branch of {question.text}: "
                f"{BRANCH_STALL} searches in a row added nothing new",
            )
        stalled = 0 if new else stalled + 1
        if new:
            judge(question)
        if question.status == "open" and question.attempts >= max_attempts:
            question.status = "unresolved"
        # Before the event, so the status that follows it carries the questions as they now stand.
        record()
        # done and total carry the claim counts here, not progress through the stage.
        stream.publish(
            stage,
            f"search {searches}: {len(new)} new claims, {len(claims)} in all; "
            f"{question.text}: {question.status}",
            len(new),
            len(claims),
            kind="result",
            parent=attempt,
        )
    return searches


def pick(questions: list[Question], max_attempts: int) -> Question | None:
    """The question to search for next: the least tried of those not settled, shallowest first.

    Least tried first, so every question gets a plain search before any gets a narrower one.
    """
    waiting = [
        q
        for q in questions
        if q.status in ("open", "weak", "contradicted") and q.attempts < max_attempts
    ]
    return min(waiting, key=lambda q: (q.attempts, q.depth, q.id), default=None)


def check(
    model: GatewayModel, encoder: Encoder, brief: str, claims: list[Claim], question: Question
) -> list[str]:
    """Judge `question` on its closest claims, and return the harder questions the judge asks.

    A claim whose quote does not say what it claims is not counted, so a misquoted answer leaves
    the question open and the next search looks for it again.
    """
    previous, question.judged_at = question.judged_at, len(claims)
    shown = [
        claims[i]
        for i in closest(encoder, question.text, [c.text for c in claims], CLAIMS_PER_QUESTION)
    ]
    # Ids count up from 1 as claims arrive, so none above `previous` means the closest claims are
    # the ones it was last judged on, and the verdict would not change.
    if all(c.id <= previous for c in shown):
        return []
    try:
        judged = predict(
            model, CheckAnswer, brief=brief, question=question.text, evidence=quoted_claims(shown)
        )
    except (AdapterParseError, ValidationError) as exc:
        logger.warning("could not read the judgement on %r: %s", question.text, exc)
        return []
    # Only ids it was shown: a small model otherwise cites claims from its own numbering.
    ids = [c.id for c in shown if c.id in judged.claims]
    if judged.status == "open" or not ids:
        question.status, question.claims = "open", []
    else:
        question.status, question.claims = judged.status, ids
    question.missing = "" if question.status == "answered" else str(judged.missing).strip()
    return [str(q) for q in judged.follow_ups] if question.status == "answered" else []
