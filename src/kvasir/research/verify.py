"""Checking each cited sentence of a report against the quotes it cites.

A small model cites claims that do not say what its sentence says. Each sentence that cites a claim
is shown with the quotes of the claims it cites, and taken out when they do not support it. A
sentence that cites nothing is left alone: it is a transition, or a statement that the evidence
does not settle something, which is what the writer is asked to say.

With a grounding checker, a sentence citing one claim is checked by it, and only a sentence
combining several claims goes to the model: tiers 2 and 3 of verification.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.research.calls import predict
from kvasir.research.claims import Claim, quoted_claims
from kvasir.research.ground import grounded
from kvasir.research.signatures import Verify
from kvasir.runs.progress import VERIFY, ProgressStream
from kvasir.schemas.results import RemovedSentence
from kvasir.storm.lm import GatewayModel

# A claim id as the writer cites it: `[3]`, or `[3, 7]`.
_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
# The end of a sentence: its punctuation, then any markers that trail it, then a space before
# what starts a sentence. Requiring a capital keeps "e.g. this" and "2.5 GB" whole.
_END = re.compile(r"(?<=[.!?])((?:\s*\[\d+(?:\s*,\s*\d+)*\])*)\s+(?=[A-Z*_(\"“\[])")
# What a line keeps when its sentences are checked: a list bullet, a number, a quote marker.
_PREFIX = re.compile(r"^(\s*(?:[-*+]|\d+\.|>)\s+)")

Check = Callable[[str, list[Claim]], tuple[bool, str]]

logger = logging.getLogger(__name__)


def report(
    model: GatewayModel,
    sections: list[tuple[str, str]],
    claims: dict[int, Claim],
    stream: ProgressStream,
    checker: GatewayModel | None = None,
) -> tuple[list[tuple[str, str]], list[RemovedSentence]]:
    """`sections` without the cited sentences found unsupported, and those sentences."""
    total = sum(count(text) for _, text in sections)
    done = 0
    stream.publish(VERIFY, f"checking {total} cited sentences against their quotes", 0, total)

    def check(sentence: str, evidence: list[Claim]) -> tuple[bool, str]:
        if checker is not None and len(evidence) == 1:
            [claim] = evidence
            if grounded(checker, f"{claim.text}\n{claim.quote}", unmarked(sentence)):
                return True, ""
            return False, "the grounding checker found the claim does not support it"
        try:
            verdict = predict(model, Verify, sentence=sentence, evidence=quoted_claims(evidence))
        except AdapterParseError as exc:
            # Unchecked rather than removed: a verdict that cannot be read says nothing about
            # the sentence.
            logger.warning("could not read the verdict on %r: %s", sentence, exc)
            return True, ""
        return bool(verdict.supported), str(verdict.reason)

    def checked() -> None:
        nonlocal done
        done += 1
        stream.publish(VERIFY, f"checked {done} of {total} cited sentences", done, total)

    verified: list[tuple[str, str]] = []
    removed: list[RemovedSentence] = []
    for title, text in sections:
        kept, dropped = verify(title, text, claims, check, checked)
        verified.append((title, kept))
        removed.extend(dropped)
    stream.publish(VERIFY, f"removed {len(removed)} of {total} cited sentences", total, total)
    return verified, removed


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


def unmarked(sentence: str) -> str:
    """`sentence` without its claim markers."""
    return " ".join(re.sub(rf"\s*{_MARKER.pattern}", "", sentence).split())


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
