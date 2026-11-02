"""Taking out of written prose the tics a model adds whatever it is told.

The writer is asked not to use them; this catches what it uses anyway. Only words that carry no
fact are removed, and nothing inside double quotes or code, so verification still sees every claim.
"""

from __future__ import annotations

import logging
import re

# Openers that announce a point instead of making it. Taken off the start of a sentence, which then
# starts at the next word.
_OPENERS = re.compile(
    r"(?<![^\s>*-])(?:It is (?:important|crucial) to note that\s+|"
    r"It(?:'s| is) worth (?:noting|mentioning) that\s+|(?:Notably|Importantly|Interestingly|"
    r"Ultimately|Essentially|Fundamentally|Crucially|In conclusion|Overall|In essence|"
    r"Indeed|Moreover|Furthermore|Additionally),\s+)([\w*])",
)
# Intensifiers, wherever they are.
_FILLER = re.compile(r"\s+\b(?:really|truly|very|actually|basically|simply|quite)\b(?=\s)", re.I)
# An em dash between numbers is a range; anywhere else, an aside a comma sets off as well.
_RANGE = re.compile(r"(?<=\d)\s*—\s*(?=\d)")
_DASH = re.compile(r"\s*—\s*")
# What is left as written: code, and anything in double quotes.
_KEPT = re.compile(r"(`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”)")

logger = logging.getLogger(__name__)


def clean(text: str) -> str:
    """`text` without filler openers, intensifiers and em dashes, outside quotes and code."""
    lines, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        lines.append(line if fenced or line.lstrip().startswith("```") else _line(line))
    cleaned = "\n".join(lines)
    if cleaned != text:
        logger.info("took filler out of the prose: %d characters", len(text) - len(cleaned))
    return cleaned


def _line(line: str) -> str:
    parts = _KEPT.split(line)
    # Odd parts are the quotes and code `_KEPT` captured.
    return "".join(part if n % 2 else _prose(part) for n, part in enumerate(parts))


def _prose(text: str) -> str:
    text = _OPENERS.sub(lambda m: m.group(1).upper(), text)
    text = _FILLER.sub("", text)
    text = _RANGE.sub("–", text)
    return _DASH.sub(", ", text)
