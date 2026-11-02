"""The report as markdown, with claim ids turned into numbered sources."""

from __future__ import annotations

import re

from kvasir.research.claims import Claim
from kvasir.schemas.results import Cell, Citation, Criterion, Row

# `[3]`, and `[3, 7]`, which models write despite being asked for `[3][7]`.
_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_REPEAT = re.compile(r"(\[\d+\])\1+")
_HEADING = re.compile(r"^(#{2,3}) +(.+?) *#*$")
# The summary's title, under which verification reports what it removed from it.
SUMMARY = "TL;DR"
# Sections a report needs before contents help a reader find their way.
CONTENTS_SECTIONS = 3


def render(
    sections: list[tuple[str, str]], claims: dict[int, Claim], summary: str = ""
) -> tuple[str, list[Citation]]:
    """The report, its sources numbered by first citation: one number per page, not per claim.

    A marker naming a claim that does not exist is dropped: the model made it up. The `summary`
    goes first, as a TL;DR callout, then the contents once there are enough sections, as callouts
    GitHub and Obsidian render and other renderers show as quotes.
    """
    numbers: dict[str, int] = {}
    citations: list[Citation] = []

    def renumber(match: re.Match[str]) -> str:
        markers = []
        for found in match.group(1).split(","):
            claim = claims.get(int(found))
            if claim is None:
                continue
            if claim.url not in numbers:
                numbers[claim.url] = len(numbers) + 1
                citations.append(
                    Citation(
                        index=numbers[claim.url],
                        url=claim.url,
                        title=claim.title,
                        snippet=claim.quote,
                    )
                )
            markers.append(f"[{numbers[claim.url]}]")
        return "".join(markers)

    def cited(text: str) -> str:
        return _REPEAT.sub(r"\1", _MARKER.sub(renumber, text))

    parts = [_callout("SUMMARY", SUMMARY, cited(summary.strip()))] if summary.strip() else []
    body = [f"## {title}\n\n" + _untitled(title, text.strip()) for title, text in sections]
    if len(sections) >= CONTENTS_SECTIONS:
        parts.append(_callout("ABSTRACT", "Contents", contents(body)))
    parts.extend(cited(text) for text in body)
    return "\n\n".join(parts), citations


def _callout(kind: str, title: str, text: str) -> str:
    return "\n".join([f"> [!{kind}] {title}"] + [f"> {line}".rstrip() for line in text.split("\n")])


def contents(sections: list[str]) -> str:
    """A nested list linking to each `##` and `###` heading, by GitHub's anchors."""
    taken: dict[str, int] = {}
    lines, fenced = [], False
    for line in "\n\n".join(sections).split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        if fenced or not (match := _HEADING.match(line)):
            continue
        title = " ".join(_MARKER.sub("", match.group(2)).split())
        indent = "  " * (len(match.group(1)) - 2)
        lines.append(f"{indent}- [{title}](#{slug(title, taken)})")
    return "\n".join(lines)


def slug(title: str, taken: dict[str, int]) -> str:
    """The anchor GitHub gives a heading, `-1`, `-2` and on added to one already taken."""
    base = re.sub(r"[^\w\s-]", "", title.lower().strip()).replace(" ", "-")
    count = taken.get(base, 0)
    taken[base] = count + 1
    return f"{base}-{count}" if count else base


def _untitled(title: str, text: str) -> str:
    """The section without a first heading repeating its title, which models add anyway."""
    first, _, rest = text.partition("\n")
    if first.startswith("#") and first.lstrip("#").strip().lower() == title.strip().lower():
        return rest.strip()
    return text


def matrix(rows: list[Row], criteria: list[Criterion]) -> str:
    """The comparison as a markdown table, its cells cited by claim id like the prose."""
    ranked = bool(rows) and rows[0].rank is not None
    header = (["Rank"] if ranked else []) + ["Option"] + [_plain(c.name) for c in criteria]
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    for row in rows:
        cells = [_cell(cell) for cell in row.cells]
        lines.append(
            "| "
            + " | ".join(([str(row.rank)] if ranked else []) + [_plain(row.entity)] + cells)
            + " |"
        )
    note = "A dash means no claim spoke to it."
    if ranked:
        weights = ", ".join(f"{c.name} {c.weight}" for c in criteria)
        note = (
            "Each criterion was ranked twice, the second time with the options in reverse order. "
            "The overall rank is the mean of those places, weighted from 1 to 3 by importance: "
            f"{weights}. {note}"
        )
    return "\n".join([*lines, "", note])


def _cell(cell: Cell) -> str:
    if not cell.claims:
        return "–"
    return _plain(cell.finding) + " " + "".join(f"[{i}]" for i in cell.claims)


def _plain(text: str) -> str:
    """`text` safe inside a table cell."""
    return " ".join(text.replace("|", "/").split())
