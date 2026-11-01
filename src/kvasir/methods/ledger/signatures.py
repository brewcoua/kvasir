"""The signatures only the question ledger uses. Each call sees one small input."""

from __future__ import annotations

from typing import Literal

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from kvasir.schemas.results import Output


class Brief(dspy.Signature):
    """Turn a research question and its clarifications into a research brief.

    The brief states what to find out, the scope, and what a good answer contains. Record every
    choice the question and the clarifications left open as an assumption. A question about what
    is best, latest or current is about the state of things on the given date.
    """

    today: str = dspy.InputField(desc="today's date")
    question: str = dspy.InputField()
    clarifications: str = dspy.InputField(desc="questions asked and the answers given, or none")
    brief: str = dspy.OutputField(desc="one paragraph")
    assumptions: list[str] = dspy.OutputField(desc="choices made on the author's behalf")
    output: Output = dspy.OutputField(
        desc="ranking if the question asks which options are best, table if it asks to compare "
        "options without ranking them, report otherwise"
    )
    criteria: list[str] = dspy.OutputField(
        desc="the criteria the question says to compare or rank on, in its words, or empty"
    )
    entities: list[str] = dspy.OutputField(
        desc="the options the question names as the full set to compare, or empty if it leaves "
        "the set open"
    )


class PlanQueries(dspy.Signature):
    """Plan the next web searches for a research brief.

    The notes say what is known and what is missing. Search for what is missing. Do not repeat a
    search the notes show was already done. When the brief is about the current state of things,
    name the current year in the query, since what you know may be out of date.
    """

    today: str = dspy.InputField(desc="today's date")
    brief: str = dspy.InputField()
    notes: str = dspy.InputField(desc="what is known so far, or empty at the start")
    count: int = dspy.InputField(desc="how many queries to return")
    queries: list[str] = dspy.OutputField(desc="search engine queries")


class NamedQuestion(BaseModel):
    """A question, and the short name a graph of the research shows it by."""

    title: str = Field(description="two to five words naming what the question asks about")
    question: str


class Decompose(dspy.Signature):
    """Split a research brief into the questions a good answer has to settle.

    Each question asks for one thing a source can state, such as a figure, a date, a cause or a
    name. Together they cover the brief. Order them from the most central to the least. Ask, do
    not answer: name no fact, example or figure the brief does not state, since what you recall
    may be wrong and would steer the search towards it.
    """

    brief: str = dspy.InputField()
    questions: list[NamedQuestion] = dspy.OutputField(desc="three to six questions")


class SearchFor(dspy.Signature):
    """Write one web search query that would find the answer to a research question.

    Write it in the shape asked for. Do not repeat a query already tried; the missing part says
    what the last search left out. When the brief is about the current state of things, name the
    current year, since what you know may be out of date.
    """

    today: str = dspy.InputField(desc="today's date")
    brief: str = dspy.InputField()
    question: str = dspy.InputField()
    tried: str = dspy.InputField(desc="queries already tried for this question, one per line")
    missing: str = dspy.InputField(desc="what the last search left out, or empty")
    shape: str = dspy.InputField(desc="the kind of query to write")
    query: str = dspy.OutputField(desc="a search engine query")


class ChosenCriterion(BaseModel):
    name: str
    weight: int


class Criteria(dspy.Signature):
    """Choose the criteria to compare options on, for a research brief.

    Include every criterion the brief names. Each is one thing a source can state about an option,
    such as a size, a price or a feature, not a judgement such as "overall quality". Weigh each
    from 1 to 3 by how much it matters to the brief.
    """

    brief: str = dspy.InputField()
    background: str = dspy.InputField(desc="search results on the topic")
    criteria: list[ChosenCriterion] = dspy.OutputField(desc="three to five criteria")


class Entities(dspy.Signature):
    """List the options a research brief compares, such as products, companies or models.

    Include every option the brief names. Then add the options the search results show are the
    main ones. An option is something the brief asks to choose between: never a benchmark, a
    leaderboard, an article or a website, and never one the brief rules out. Name each the way the
    sources do, shortly.
    """

    brief: str = dspy.InputField()
    background: str = dspy.InputField(desc="search results on the topic")
    count: int = dspy.InputField(desc="the most options to return")
    entities: list[str] = dspy.OutputField()


class CheckAnswer(dspy.Signature):
    """Decide how well the evidence answers one research question.

    Count a claim only if its quote states what the claim says. answered: the counted claims
    settle the question. weak: they settle part of it. contradicted: they disagree with each
    other. open: no claim speaks to it. When answered, suggest harder questions an expert would
    ask next about the same subject, which the brief would gain from.
    """

    brief: str = dspy.InputField()
    question: str = dspy.InputField()
    evidence: str = dspy.InputField(desc="each claim with the quote it was read from")
    status: Literal["answered", "weak", "contradicted", "open"] = dspy.OutputField()
    claims: list[int] = dspy.OutputField(desc="ids of the claims counted as support")
    missing: str = dspy.OutputField(desc="what is still missing, in a few words, or empty")
    follow_ups: list[str] = dspy.OutputField(desc="at most two harder questions, or empty")


class Assess(dspy.Signature):
    """State what the evidence says about one option on one criterion.

    Use only claims about this option. The finding is a table cell: at most six words, without the
    option's name, such as "24 GB at 4-bit" or "Apache 2.0". Return an empty finding and no claim
    ids if no claim speaks to it.
    """

    brief: str = dspy.InputField()
    entity: str = dspy.InputField(desc="the option")
    criterion: str = dspy.InputField()
    evidence: str = dspy.InputField(desc="one claim per line, prefixed by its id in brackets")
    finding: str = dspy.OutputField(desc="at most six words, or empty")
    claims: list[int] = dspy.OutputField(desc="ids of the claims the finding rests on")


class RankCriterion(dspy.Signature):
    """Rank options on one criterion, best first, from what the evidence says about each."""

    brief: str = dspy.InputField()
    criterion: str = dspy.InputField()
    options: str = dspy.InputField(desc="one option per line, with its finding")
    ranking: list[str] = dspy.OutputField(desc="every option's name, best first")


class Outline(dspy.Signature):
    """Outline a research report answering a brief from research notes, in the shape asked for."""

    brief: str = dspy.InputField()
    shape: str = dspy.InputField(desc="how the report is organised")
    notes: str = dspy.InputField()
    sections: list[str] = dspy.OutputField(desc="section titles, three to seven")
