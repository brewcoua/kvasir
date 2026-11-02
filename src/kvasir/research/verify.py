"""Checking each cited sentence of a report against the quotes it cites.

A small model cites claims that do not say what its sentence says. Each sentence that cites a claim
is checked against the quotes of the claims it cites, by a cascade that spends the dearer models
only on the sentences the cheaper ones leave in doubt:

1. A sentence citing no claim the research gathered is removed.
2. The grounding checker, if there is one, keeps a sentence it supports.
3. The fast model removes a sentence it finds unsupported. Without a checker it also keeps one it
   finds supported; with one, a disagreement goes on.
4. The reason model has the last word: it keeps, removes, or rewrites a sentence that is only partly
   supported. A verdict it cannot give leaves the sentence in, marked `[unverified]`.
5. A rewrite must pass the checker, or the fast model without one, or it is removed too.

Each step runs over every sentence before the next begins: the checker and the fast model share one
local server, and alternating between them would load a model for each sentence. A sentence that
cites nothing is left alone: it is a transition, or a statement that the evidence does not settle
something, which is what the writer is asked to say. Tier 2 of verification.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.research.calls import predict
from kvasir.research.claims import Claim, quoted_claims
from kvasir.research.ground import grounded
from kvasir.research.signatures import Escalate, Verify
from kvasir.runs.progress import VERIFY, ProgressStream
from kvasir.schemas.results import RemovedSentence, RevisedSentence, Stage, UnverifiedSentence
from kvasir.storm.lm import GatewayModel

# Appended to a sentence no step could judge.
UNVERIFIED = "[unverified]"
# A claim id as the writer cites it: `[3]`, or `[3, 7]`.
_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_UNVERIFIED = re.escape(UNVERIFIED)
# The end of a sentence: its punctuation, then any markers that trail it, then a space before
# what starts a sentence. Requiring a capital keeps "e.g. this" and "2.5 GB" whole.
_END = re.compile(
    rf"(?<=[.!?])((?:\s*(?:\[\d+(?:\s*,\s*\d+)*\]|{_UNVERIFIED}))*)\s+(?=[A-Z*_(\"“\[])"
)
# What a line keeps when its sentences are checked: a list bullet, a number, a quote marker.
_PREFIX = re.compile(r"^(\s*(?:[-*+]|\d+\.|>)\s+)")
# The first line of a callout, `> [!WARNING]` and its title.
_CALLOUT = re.compile(r"^\s*> ?\[!\w+\]")

logger = logging.getLogger(__name__)


@dataclass
class Verdict:
    """What the cascade decided about one sentence, and the step that decided it.

    `text` is the sentence to keep: as given, or rewritten when `status` is "rewritten".
    """

    status: Literal["kept", "rewritten", "removed", "unverified"]
    text: str
    stage: Stage
    reason: str = ""


@dataclass
class Verified:
    """Sections after verification, and what it did to their sentences."""

    sections: list[tuple[str, str]]
    removed: list[RemovedSentence]
    revised: list[RevisedSentence]
    unverified: list[UnverifiedSentence]


def report(
    models: dict[str, GatewayModel],
    sections: list[tuple[str, str]],
    claims: dict[int, Claim],
    stream: ProgressStream,
) -> Verified:
    """`sections` with each cited sentence kept, rewritten, marked or removed by the cascade.

    Every section's cited sentences go through the cascade together, so each step loads its model
    once. Headings, tables and code are left as they are: a table row is not a sentence, and
    cutting one would break the table.
    """
    items: list[tuple[str, list[Claim]]] = []

    def collect(sentence: str) -> str:
        items.append((sentence, [claims[i] for i in dict.fromkeys(cited(sentence)) if i in claims]))
        return sentence

    for _, text in sections:
        edit(text, collect)
    verdicts = iter(cascade(models, items, stream))
    done = Verified([], [], [], [])
    for title, text in sections:
        done.sections.append((title, edit(text, partial(_apply, done, verdicts, title))))
    stream.publish(
        VERIFY,
        f"removed {len(done.removed)} of {len(items)} cited sentences",
        len(items),
        len(items),
    )
    return done


def _apply(done: Verified, verdicts: Iterator[Verdict], title: str, sentence: str) -> str:
    """What the next verdict makes of `sentence`, recorded in `done`; empty if it is removed."""
    verdict = next(verdicts)
    match verdict.status:
        case "removed":
            done.removed.append(
                RemovedSentence(
                    section=title, sentence=sentence, reason=verdict.reason, stage=verdict.stage
                )
            )
            return ""
        case "rewritten":
            done.revised.append(
                RevisedSentence(
                    section=title, before=sentence, after=verdict.text, reason=verdict.reason
                )
            )
        case "unverified":
            done.unverified.append(
                UnverifiedSentence(section=title, sentence=sentence, reason=verdict.reason)
            )
            return f"{verdict.text} {UNVERIFIED}"
    return verdict.text


def cascade(
    models: dict[str, GatewayModel],
    items: list[tuple[str, list[Claim]]],
    stream: ProgressStream,
    stage: str = VERIFY,
) -> list[Verdict]:
    """A verdict on each sentence against the claims it cites, in order.

    A sentence with no claims to check against cites nothing the research gathered. Without a
    grounding checker in `models`, the fast model is the first to judge. Progress is published under
    `stage`, for a caller that runs the cascade outside verification.
    """
    total = len(items)
    verdicts: dict[int, Verdict] = {}
    checker = models.get("ground")

    def settle(
        i: int,
        status: Literal["kept", "rewritten", "removed", "unverified"],
        decider: Stage,
        reason: str = "",
        text: str | None = None,
    ) -> None:
        verdicts[i] = Verdict(status, items[i][0] if text is None else text, decider, reason)
        stream.publish(
            stage, f"checked {len(verdicts)} of {total} cited sentences", len(verdicts), total
        )

    def begin(step: str, pending: list[int]) -> None:
        stream.publish(stage, f"{step}: checking {len(pending)} of {total} sentences")

    def claim_of(i: int) -> str:
        # Bold marks a key term for the reader, and is no part of what is checked.
        return items[i][0].replace("**", "")

    def holds(i: int, sentence: str) -> bool:
        evidence = items[i][1]
        if checker is not None:
            document = "\n\n".join(f"{claim.text}\n{claim.quote}" for claim in evidence)
            return grounded(checker, document, unmarked(sentence))
        answer = _ask(models["verify"], Verify, sentence=sentence, evidence=quoted_claims(evidence))
        return answer is not None and answer.verdict == "supported"

    stream.publish(stage, f"checking {total} cited sentences against their quotes", 0, total)
    pending = []
    for i, (_, evidence) in enumerate(items):
        if evidence:
            pending.append(i)
        else:
            settle(i, "removed", "citation", "cites no claim the research gathered")

    if checker is not None and pending:
        begin("checker", pending)
        unsupported = []
        for i in pending:
            if holds(i, claim_of(i)):
                settle(i, "kept", "checker")
            else:
                unsupported.append(i)
        pending = unsupported

    if pending:
        begin("fast model", pending)
    doubtful = []
    for i in pending:
        answer = _ask(
            models["verify"], Verify, sentence=claim_of(i), evidence=quoted_claims(items[i][1])
        )
        # A verdict that cannot be read says nothing about the sentence, so it counts as partial.
        verdict = answer.verdict if answer is not None else "partial"
        if verdict == "unsupported":
            settle(i, "removed", "verify", str(answer.reason))
        elif verdict == "supported" and checker is None:
            settle(i, "kept", "verify")
        else:
            doubtful.append(i)

    if doubtful:
        begin("reason model", doubtful)
    rewrites: dict[int, tuple[str, str]] = {}
    for i in doubtful:
        answer = _ask(
            models["escalate"], Escalate, sentence=claim_of(i), evidence=quoted_claims(items[i][1])
        )
        if answer is None:
            settle(i, "unverified", "escalate", "the reason model's verdict could not be read")
        elif answer.verdict == "supported":
            settle(i, "kept", "escalate")
        elif answer.verdict == "unsupported":
            settle(i, "removed", "escalate", str(answer.reason))
        else:
            rewrites[i] = (str(answer.rewrite).strip(), str(answer.reason))

    if rewrites:
        begin("rewrites", list(rewrites))
    for i, (rewrite, reason) in rewrites.items():
        allowed = {claim.id for claim in items[i][1]}
        if not rewrite:
            settle(i, "removed", "rewrite", f"the reason model gave no rewrite ({reason})")
        elif set(cited(rewrite)) - allowed:
            settle(i, "removed", "rewrite", "the rewrite cites a claim the sentence did not")
        elif holds(i, rewrite.replace("**", "")):
            settle(i, "rewritten", "rewrite", reason, rewrite)
        else:
            settle(i, "removed", "rewrite", f"the rewrite is not supported either ({reason})")
    return [verdicts[i] for i in range(total)]


def _ask(model: GatewayModel, signature: Any, **inputs: Any) -> Any:
    """The model's answer, or None when its verdict cannot be read."""
    try:
        return predict(model, signature, **inputs)
    except AdapterParseError as exc:
        logger.warning("could not read the verdict on %r: %s", inputs["sentence"], exc)
        return None


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
    """`sentence` without its claim markers and its `[unverified]` mark."""
    return " ".join(re.sub(rf"\s*(?:{_MARKER.pattern}|{_UNVERIFIED})", "", sentence).split())


def edit(text: str, decide: Callable[[str], str]) -> str:
    """`text` with each cited sentence replaced by what `decide` returns for it, in order.

    A sentence `decide` returns nothing for is removed. A list item with nothing left goes
    entirely, rather than leaving an empty bullet, and a callout with every sentence removed goes
    too, rather than leaving its title alone.
    """
    edited: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or line.lstrip().startswith(("```", "#", "|")) or not _MARKER.search(line):
            edited.append(line)
            continue

        prefix_match = _PREFIX.match(line)
        prefix = prefix_match.group(1) if prefix_match else ""
        kept: list[str] = []
        for sentence in sentences(line[len(prefix) :]):
            if not cited(sentence):
                kept.append(sentence)
            elif replacement := decide(sentence.strip()):
                kept.append(replacement)
        if kept:
            edited.append(prefix + " ".join(part.strip() for part in kept))
    return "\n".join(
        line
        for line, after in zip(edited, [*edited, ""][1:], strict=True)
        if not _CALLOUT.match(line) or after.lstrip().startswith(">")
    )
