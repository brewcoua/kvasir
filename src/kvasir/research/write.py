"""Writing one section of a report from the claims closest to it, and the report's summary."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]
from pydantic import ValidationError

from kvasir.research.calls import predict
from kvasir.research.claims import Claim, closest, listed
from kvasir.research.read import page_characters
from kvasir.research.signatures import Summarize, WriteSection
from kvasir.research.style import clean
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel
from kvasir.storm.runtime import context_tokens

logger = logging.getLogger(__name__)

# Claims given to one section, the closest to its title.
CLAIMS_PER_SECTION = 15
# How much of each earlier section a later one sees, to avoid repeating it without filling the
# context with it.
EARLIER_SECTION_CHARACTERS = 400


def section(
    model: GatewayModel,
    encoder: Encoder,
    brief: str,
    titles: list[str],
    title: str,
    earlier: list[tuple[str, str]],
    claims: list[Claim],
    extra: Iterable[int] = (),
    subsections: Sequence[str] = (),
) -> str:
    """One section, from the claims closest to its title and the `extra` claim ids.

    Empty when the answer cannot be read, such as a thinking model spending its whole budget on
    reasoning, so one section does not end the run.
    """
    shown = closest(encoder, f"{title}\n{brief}", [c.text for c in claims], CLAIMS_PER_SECTION)
    ids = {claims[i].id for i in shown} | set(extra)
    try:
        text = predict(
            model,
            WriteSection,
            brief=brief,
            outline="\n".join(titles),
            section=title,
            subsections="\n".join(subsections),
            earlier_sections="\n\n".join(
                f"{t}: {body[:EARLIER_SECTION_CHARACTERS]}" for t, body in earlier
            ),
            evidence=listed([c for c in claims if c.id in ids]),
        ).text
    except (AdapterParseError, ValidationError) as exc:
        logger.warning("could not read section %s: %s", title, exc)
        return ""
    return clean(text)


def summary(model: GatewayModel, brief: str, sections: list[tuple[str, str]]) -> str:
    """The report's main findings as a cited list, or nothing when they cannot be read.

    Shown the start of each section, as much as the context holds shared between them, since a
    section leads with its finding.
    """
    each = page_characters(context_tokens()) // max(len(sections), 1)
    report = "\n\n".join(f"## {title}\n{text[:each]}" for title, text in sections)
    try:
        points = predict(model, Summarize, brief=brief, report=report).points
    except (AdapterParseError, ValidationError) as exc:
        logger.warning("could not read the summary: %s", exc)
        return ""
    return "\n".join(f"- {clean(' '.join(point.split()))}" for point in points if point.strip())
