"""Filling a ranking or a table: one finding per option and criterion, then ranks."""

from __future__ import annotations

import logging
import re
from statistics import mean
from typing import Any

from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.methods.ledger.loop import Research
from kvasir.methods.ledger.questions import Question
from kvasir.methods.ledger.signatures import Assess, RankCriterion
from kvasir.research.calls import predict
from kvasir.research.claims import Claim, listed, nearest
from kvasir.research.text import names
from kvasir.research.verify import UNVERIFIED, cascade
from kvasir.runs.progress import ASSESS, ProgressStream
from kvasir.schemas.results import Cell, Criterion, RemovedSentence, Row, UnverifiedSentence
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel

# Claims shown when assessing one option on one criterion, the closest to the pair.
CLAIMS_PER_CELL = 8
# Searches for the cells still empty after the research, on top of its budget.
FILL_SEARCHES = 8

logger = logging.getLogger(__name__)


def assess(
    models: dict[str, GatewayModel],
    encoder: Encoder,
    brief: str,
    claims: list[Claim],
    entities: list[str],
    criteria: list[Criterion],
    stream: ProgressStream,
) -> list[Row]:
    """Each option's finding on each criterion, from the claims the research gathered."""
    rows = [Row(entity=entity, cells=[]) for entity in entities]
    total = len(entities) * len(criteria)
    vectors = encoder.encode([c.text for c in claims])
    for done, (row, criterion) in enumerate((row, c) for row in rows for c in criteria):
        stream.publish(ASSESS, f"assessing {row.entity} on {criterion.name}", done, total)
        row.cells.append(
            _cell(models["assess"], encoder, vectors, brief, claims, row.entity, criterion.name)
        )
    return rows


def fill(
    model: GatewayModel,
    encoder: Encoder,
    brief: str,
    claims: list[Claim],
    ledger: list[Question],
    rows: list[Row],
    research: Research,
    stream: ProgressStream,
) -> None:
    """Search for the empty cells of `rows` as questions of their own, and assess them again.

    A cell's question is answered once assessing it finds a claim, so the assessment is its
    judgement and no other call is needed.
    """
    cells = {
        (row.entity, cell.criterion): (row, i) for row in rows for i, cell in enumerate(row.cells)
    }
    empty = [key for key, (row, i) in cells.items() if not row.cells[i].claims]
    if not empty:
        return
    questions = [
        Question(
            id=len(ledger) + n,
            text=f"{criterion} of {entity}",
            cell=(entity, criterion),
            judged_at=len(claims),
        )
        for n, (entity, criterion) in enumerate(empty, 1)
    ]
    ledger.extend(questions)

    def judge(question: Question) -> None:
        assert question.cell is not None
        row, i = cells[question.cell]
        vectors = encoder.encode([c.text for c in claims])
        row.cells[i] = _cell(model, encoder, vectors, brief, claims, *question.cell)
        question.judged_at = len(claims)
        question.claims = row.cells[i].claims
        question.status = "answered" if question.claims else "open"

    stream.publish(ASSESS, f"searching for {len(empty)} empty cells")
    research(questions, FILL_SEARCHES, judge)
    filled = sum(1 for key in empty if cells[key][0].cells[cells[key][1]].claims)
    stream.publish(ASSESS, f"filled {filled} of {len(empty)} empty cells")


def _cell(
    model: GatewayModel,
    encoder: Encoder,
    vectors: Any,
    brief: str,
    claims: list[Claim],
    entity: str,
    criterion: str,
) -> Cell:
    shown = [
        claims[i] for i in nearest(encoder, f"{entity}: {criterion}", vectors, CLAIMS_PER_CELL)
    ]
    try:
        assessed = predict(
            model, Assess, brief=brief, entity=entity, criterion=criterion, evidence=listed(shown)
        )
    except AdapterParseError as exc:
        logger.warning("could not read the finding on %s, %s: %s", entity, criterion, exc)
        return Cell(criterion=criterion, finding="", claims=[])
    # Only ids it was shown, of claims that name the entity: a small model otherwise cites another
    # option's figures for this one.
    ids = [
        c.id
        for c in shown
        if c.id in assessed.claims and names(entity, f"{c.text} {c.quote} {c.title}")
    ]
    finding = str(assessed.finding).strip()
    if not (ids and finding):
        return Cell(criterion=criterion, finding="", claims=[])
    return Cell(criterion=criterion, finding=finding, claims=ids)


def verify_cells(
    models: dict[str, GatewayModel],
    rows: list[Row],
    by_id: dict[int, Claim],
    stream: ProgressStream,
    title: str,
) -> tuple[list[RemovedSentence], list[UnverifiedSentence]]:
    """Check each cell with a finding against the claims it cites, and empty or mark the doubtful.

    A table is not a sentence to cut, so verifying the report leaves it alone; its cells go through
    the same cascade here, all together so each model loads once. A cell is at most six words, so a
    partly supported one is emptied rather than rewritten. An emptied cell ties for last when
    ranked, like any cell no claim spoke to.
    """
    cells = [(row, cell) for row in rows for cell in row.cells if cell.claims]
    if not cells:
        return [], []
    items = [
        (
            f"{row.entity}, {cell.criterion}: {cell.finding} "
            + "".join(f"[{i}]" for i in cell.claims),
            [by_id[i] for i in cell.claims],
        )
        for row, cell in cells
    ]
    stream.publish(ASSESS, f"checking {len(cells)} cells against their quotes")
    verdicts = cascade(models, items, stream, ASSESS)
    removed: list[RemovedSentence] = []
    unverified: list[UnverifiedSentence] = []
    for (text, _), (_, cell), verdict in zip(items, cells, verdicts, strict=True):
        if verdict.status in ("removed", "rewritten"):
            reason = verdict.reason
            if verdict.status == "rewritten":
                reason = f"a partly supported cell is removed rather than rewritten ({reason})"
            removed.append(
                RemovedSentence(section=title, sentence=text, reason=reason, stage=verdict.stage)
            )
            cell.finding, cell.claims = "", []
        elif verdict.status == "unverified":
            unverified.append(
                UnverifiedSentence(section=title, sentence=text, reason=verdict.reason)
            )
            cell.finding += f" {UNVERIFIED}"
    return removed, unverified


def rank(
    model: GatewayModel,
    brief: str,
    criteria: list[Criterion],
    rows: list[Row],
    stream: ProgressStream,
) -> list[Row]:
    """`rows` best first, each ranked on every criterion twice, the second time in reverse order.

    A model favours the options it reads first; reversing the order and averaging cancels that.
    Only options with a finding are judged; those without one tie for the places after them, since
    a judge asked to rank them anyway places them by guesswork. The overall order is the weighted
    mean of the criterion ranks, computed here, not judged.
    """
    for index, criterion in enumerate(criteria):
        found = [row for row in rows if row.cells[index].claims]
        places: dict[str, list[int]] = {row.entity: [] for row in found}
        if len(found) == 1:
            places[found[0].entity].append(1)
        passes = (found, found[::-1]) if len(found) > 1 else ()
        for turn, order in enumerate(passes, 1):
            stream.publish(
                ASSESS,
                f"ranking on {criterion.name}, pass {turn} of 2",
                2 * index + turn - 1,
                2 * len(criteria),
            )
            # The mark is for the reader; the judge would read it as part of the finding.
            options = "\n".join(
                f"{row.entity}: {row.cells[index].finding.removesuffix(f' {UNVERIFIED}')}"
                for row in order
            )
            try:
                ranking = predict(
                    model, RankCriterion, brief=brief, criterion=criterion.name, options=options
                ).ranking
            except AdapterParseError as exc:
                # The input order stands in; the other pass, reversed, cancels it into a tie.
                logger.warning("could not read the ranking on %s: %s", criterion.name, exc)
                ranking = []
            for place, entity in enumerate(ordered(ranking, [r.entity for r in order]), 1):
                places[entity].append(place)
        last = (len(found) + 1 + len(rows)) / 2
        for row in rows:
            row.cells[index].rank = mean(places[row.entity]) if row.entity in places else last

    weight = sum(c.weight for c in criteria)
    scores = {
        row.entity: sum(
            c.weight * (cell.rank or 0) for c, cell in zip(criteria, row.cells, strict=True)
        )
        / weight
        for row in rows
    }
    ranked = sorted(rows, key=lambda row: scores[row.entity])
    for place, row in enumerate(ranked, 1):
        row.rank = place
    stream.publish(
        ASSESS,
        f"ranked: {', '.join(row.entity for row in ranked)}",
        2 * len(criteria),
        2 * len(criteria),
    )
    return ranked


def ordered(ranking: list[str], names: list[str]) -> list[str]:
    """`names` in the order `ranking` gives them, those it leaves out last, in their own order.

    Models number the list and change the case, so a name is matched without either.
    """
    known = {name.lower(): name for name in names}
    found = [
        known[key]
        for item in ranking
        if (key := re.sub(r"^\s*\d+[.)]\s*", "", item).strip().lower()) in known
    ]
    found = list(dict.fromkeys(found))
    return found + [name for name in names if name not in found]
