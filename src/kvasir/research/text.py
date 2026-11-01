"""Matching text word for word, as quotes and names are matched."""

from __future__ import annotations

import re


def quoted(quote: str, text: str) -> bool:
    """Whether `quote` is in `text` word for word, an ellipsis standing for words left out.

    Words only: models write a non-breaking hyphen where the page has a hyphen, join list items
    without their bullets, and the extracted text has a break wherever the page had an inline tag,
    as in "attention , a". None of that changes what the quote says.
    """
    haystack = words(text)
    fragments = [words(part) for part in re.split(r"\.\.\.|…", quote)]
    fragments = [fragment for fragment in fragments if fragment]
    if not fragments:
        return False
    position = 0
    for fragment in fragments:
        found = haystack.find(fragment, position)
        if found < 0:
            return False
        # Back one, so the next fragment can start at the space this one ended on.
        position = found + len(fragment) - 1
    return True


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
