"""The report as markdown, with claim ids turned into numbered sources."""

from __future__ import annotations

import re

from kvasir.deep.store import Claim
from kvasir.models import Citation

# `[3]`, and `[3, 7]`, which models write despite being asked for `[3][7]`.
_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_REPEAT = re.compile(r"(\[\d+\])\1+")


def render(sections: list[tuple[str, str]], claims: dict[int, Claim]) -> tuple[str, list[Citation]]:
    """Number sources by first citation, one number per page rather than per claim.

    A marker naming a claim that does not exist is dropped: the model made it up.
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

    body = "\n\n".join(
        f"## {title}\n\n" + _REPEAT.sub(r"\1", _MARKER.sub(renumber, text.strip()))
        for title, text in sections
    )
    return body, citations
