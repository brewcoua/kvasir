"""Matching text word for word, as quotes and names are matched."""

from __future__ import annotations

import re
from itertools import pairwise

# Words that reverse or condition what follows. A quote that starts right after one, or that skips
# one with an ellipsis, can say the opposite of the page.
QUALIFIERS = {"not", "no", "never", "unless", "if", "except", "without", "only", "cannot"}
# The line under a markdown table's header: "|---|:--:|".
_SEPARATOR = re.compile(r"\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?")


def quoted(quote: str, text: str) -> bool:
    """Whether `quote` is in `text` word for word, an ellipsis standing for words left out.

    Words only: models write a non-breaking hyphen where the page has a hyphen, join list items
    without their bullets, and the extracted text has a break wherever the page had an inline tag,
    as in "attention , a". None of that changes what the quote says.
    """
    return placed(quote, text) is not None


def misleading(quote: str, text: str) -> bool:
    """Whether a quote found in `text` can say what the page does not: the words just before it,
    or the ones an ellipsis stands for, include a negator or qualifier.

    "grows" quoted from "it never grows" is on the page, and means the opposite. The words before it
    are taken from its own sentence only: "It is not. Cats grow" does not turn "Cats grow" around.
    """
    # "isn't" as "isn not", since `words` cuts it into "isn" and "t", like the "T" of "T cells".
    text = re.sub(r"n['’]t\b", "n not", text)
    found = placed(quote, text)
    if not found:
        return False
    haystack = words(text)
    skipped = [haystack[end : start + 1].split() for (_, end), (start, _) in pairwise(found)]
    # The page's text up to the quote's first word, which `words` would have stripped of the full
    # stop that ends the sentence before it.
    lowered = text.lower()
    tokens = list(re.finditer(r"\w+", lowered))
    first = len(haystack[: found[0][0]].split())
    sentence = re.split(r"[.!?;]\s", lowered[: tokens[first].start()])[-1]
    before = words(sentence).split()[-2:]
    return any(word in QUALIFIERS for part in [before, *skipped] for word in part)


def placed(quote: str, text: str) -> list[tuple[int, int]] | None:
    """Where each fragment of `quote` is in `words(text)`, as start and end, or None when a
    fragment is not there."""
    haystack = words(text)
    fragments = [words(part) for part in re.split(r"\.\.\.|…", quote)]
    fragments = [fragment for fragment in fragments if fragment]
    if not fragments:
        return None
    spans = []
    position = 0
    for fragment in fragments:
        found = haystack.find(fragment, position)
        if found < 0:
            return None
        spans.append((found, found + len(fragment)))
        # Back one, so the next fragment can start at the space this one ended on.
        position = found + len(fragment) - 1
    return spans


def with_table_header(quote: str, text: str) -> str:
    """`quote` with the header row of the markdown table it is a row of put before it, after an
    ellipsis, so "Cold start 1.5ms 3ms 2ms" says which runtime each figure is for. Still on the
    page word for word. A quote outside a table, or one that has the header, is returned as is."""
    block: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line.lstrip().startswith("|"):
            block.append(line.strip())
            continue
        if len(block) > 2 and _SEPARATOR.fullmatch(block[1]):
            header, rows = block[0], "\n".join(block[2:])
            if quoted(quote, rows) and not quoted(quote, header):
                return f"{header} ... {quote}"
        block = []
    return quote


def names(entity: str, text: str) -> bool:
    """Whether `text` names `entity`: most of the words of its name appear in it.

    Most rather than all, since sources shorten names: "Qwen3-Coder-30B" for "Qwen3-Coder-30B-A3B".
    """
    parts = words(entity).split()
    found = sum(f" {word} " in words(text) for word in parts)
    return bool(parts) and found * 2 > len(parts)


def words(text: str) -> str:
    # Padded, so a fragment matches whole words only: "4 bit" is not found in "14 bits".
    found = re.findall(r"\w+", text.lower())
    return f" {' '.join(found)} "
