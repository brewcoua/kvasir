"""The question loop: search for one question at a time, judging each on what arrives."""

from __future__ import annotations

import logging
from collections.abc import Callable

from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.methods.ledger.questions import Question
from kvasir.methods.ledger.signatures import CheckAnswer, SearchFor
from kvasir.research.calls import predict
from kvasir.research.claims import Claim, closest, quoted_claims
from kvasir.research.read import read
from kvasir.research.search import SearchUnavailable, search
from kvasir.runs.progress import RESEARCH, ProgressStream
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
# Searches for questions within a budget of searches, judging each with the judge given.
Research = Callable[[list[Question], int, Judge], None]


def research(
    models: dict[str, GatewayModel],
    encoder: Encoder,
    searxng: SearXNG,
    today: str,
    brief: str,
    questions: list[Question],
    claims: list[Claim],
    seen: set[str],
    budget: int,
    per_query: int,
    max_attempts: int,
    judge: Judge,
    first_step: int,
    stream: ProgressStream,
) -> int:
    """Search for `questions` one at a time, and return how many searches that took.

    Stops when `budget` searches are spent, no question is left to search for, or
    `STALL_SEARCHES` searches in a row find nothing. A question is judged again before it is
    searched for whenever claims arrived since its last judgement, since a search for one question
    often answers another. Adds what it finds to `claims`; `judge` may add to `questions`.
    """
    searches = stalled = failed = 0
    failures: list[str] = []
    while searches < budget and stalled < STALL_SEARCHES:
        question = pick(questions, max_attempts)
        if question is None:
            break
        if question.judged_at < len(claims):
            judge(question)
            continue
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
            models.get("ground"),
        )
        claims.extend(new)
        stalled = 0 if new else stalled + 1
        if new:
            judge(question)
        if question.status == "open" and question.attempts >= max_attempts:
            question.status = "unresolved"
        stream.publish(
            RESEARCH,
            f"search {searches}: {len(new)} new claims, {len(claims)} in all; "
            f"{question.text}: {question.status}",
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
    except AdapterParseError as exc:
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
