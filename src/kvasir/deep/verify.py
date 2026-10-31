"""Checking each cited sentence of a report against the quotes it cites.

A small model cites claims that do not say what its sentence says. Each sentence that cites a claim
is shown with the quotes of the claims it cites, and taken out when they do not support it. A
sentence that cites nothing is left alone: it is a transition, or a statement that the evidence
does not settle something, which is what the writer is asked to say.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from kvasir.deep.store import Claim
from kvasir.models import RemovedSentence

# A claim id as the writer cites it: `[3]`, or `[3, 7]`.
_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
# The end of a sentence: its punctuation, then any markers that trail it, then a space before
# what starts a sentence. Requiring a capital keeps "e.g. this" and "2.5 GB" whole.
_END = re.compile(r"(?<=[.!?])((?:\s*\[\d+(?:\s*,\s*\d+)*\])*)\s+(?=[A-Z*_(\"“\[])")
# What a line keeps when its sentences are checked: a list bullet, a number, a quote marker.
_PREFIX = re.compile(r"^(\s*(?:[-*+]|\d+\.|>)\s+)")

Check = Callable[[str, list[Claim]], tuple[bool, str]]


def sentences(line: str) -> list[str]:
    """`line` cut into sentences, each keeping the markers that follow its full stop."""
    parts: list[str] = []
    start = 0
    for match in _END.finditer(line):
        parts.append(line[start : match.end(1)])
        start = match.end()
    parts.append(line[start:])
    return [part for part in parts if part.strip()]


def cited(sentence: str) -> list[int]:
    return [
        int(found) for match in _MARKER.finditer(sentence) for found in match.group(1).split(",")
    ]


def verify(
    title: str,
    text: str,
    claims: dict[int, Claim],
    check: Check,
    checked: Callable[[], None] = lambda: None,
) -> tuple[str, list[RemovedSentence]]:
    """`text` without the sentences its evidence does not support, and those sentences.

    Headings, tables and code are left as they are: a table row is not a sentence, and cutting one
    would break the table. `checked` is called after each cited sentence.
    """
    removed: list[RemovedSentence] = []
    kept_lines: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or line.lstrip().startswith(("```", "#", "|")) or not _MARKER.search(line):
            kept_lines.append(line)
            continue

        prefix_match = _PREFIX.match(line)
        prefix = prefix_match.group(1) if prefix_match else ""
        kept: list[str] = []
        for sentence in sentences(line[len(prefix) :]):
            ids = cited(sentence)
            if not ids:
                kept.append(sentence)
                continue
            evidence = [claims[i] for i in dict.fromkeys(ids) if i in claims]
            if not evidence:
                supported, reason = False, "cites no claim the research gathered"
            else:
                supported, reason = check(sentence.strip(), evidence)
            checked()
            if supported:
                kept.append(sentence)
            else:
                removed.append(
                    RemovedSentence(section=title, sentence=sentence.strip(), reason=reason)
                )
        # A list item with nothing left goes entirely, rather than leaving an empty bullet.
        if kept:
            kept_lines.append(prefix + " ".join(part.strip() for part in kept))
    return "\n".join(kept_lines), removed


def count(text: str) -> int:
    """How many sentences of `text` cite something, for a progress total."""
    return sum(
        1
        for line in text.split("\n")
        if not line.lstrip().startswith(("```", "#", "|"))
        for sentence in sentences(line)
        if _MARKER.search(sentence)
    )
