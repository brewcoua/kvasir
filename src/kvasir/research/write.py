"""Writing one section of a report from the claims closest to it."""

from __future__ import annotations

from collections.abc import Iterable

from kvasir.research.calls import predict
from kvasir.research.claims import Claim, closest, listed
from kvasir.research.signatures import WriteSection
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel

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
) -> str:
    """One section, from the claims closest to its title and the `extra` claim ids."""
    shown = closest(encoder, f"{title}\n{brief}", [c.text for c in claims], CLAIMS_PER_SECTION)
    ids = {claims[i].id for i in shown} | set(extra)
    return str(
        predict(
            model,
            WriteSection,
            brief=brief,
            outline="\n".join(titles),
            section=title,
            earlier_sections="\n\n".join(
                f"{t}: {body[:EARLIER_SECTION_CHARACTERS]}" for t, body in earlier
            ),
            evidence=listed([c for c in claims if c.id in ids]),
        ).text
    )
