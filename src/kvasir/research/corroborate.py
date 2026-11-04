"""Which claims other sites agree with, and which they contradict: tiers 4 and 5 of verification.

A quote on its page makes a claim one site's word. Another site saying the same makes it more.
Each claim is compared only with the claims closest to it in meaning from other sites, since
comparing every pair would cost a call for each of the tens of thousands of pairs a run holds.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import numpy as np
from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]
from pydantic import ValidationError
from tld import get_fld

from kvasir.research.calls import predict
from kvasir.research.claims import Claim, quoted_claims
from kvasir.research.ground import grounded
from kvasir.research.signatures import Contradicts, Verify
from kvasir.runs.progress import VERIFY, ProgressStream
from kvasir.storm import runtime
from kvasir.storm.encoder import Encoder, cosine_similarity
from kvasir.storm.lm import GatewayModel

# Claims from other sites each claim is compared with, at most.
NEIGHBOURS = 3
# Below this cosine similarity, two claims are taken to be about different things.
SAME_FACT = 0.8
# Words a quote needs before another quote holding it counts as a copy: a figure or a name quoted
# by two sites is two sources.
COPY_WORDS = 8

logger = logging.getLogger(__name__)


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.")


def registrable(url: str) -> str:
    """The domain a site is registered under, so `blog.a.org` and `a.org` are one source; the
    host when it has none, as an IP address or `localhost`."""
    return get_fld(url, fail_silently=True) or domain(url)


def copies(claim: Claim, other: Claim) -> bool:
    """Whether the quotes of `claim` and `other` are one text, or one of `COPY_WORDS` words or
    more holds the other."""
    a, b = sorted((" ".join(c.quote.casefold().split()) for c in (claim, other)), key=len)
    return bool(a) and (a == b or (len(a.split()) >= COPY_WORDS and a in b))


def independent(claim: Claim, other: Claim) -> bool:
    """Whether `other` is a second source for `claim`: another registrable domain, and not a copy
    of its quote."""
    return registrable(claim.url) != registrable(other.url) and not copies(claim, other)


def cross_check(
    models: dict[str, GatewayModel],
    encoder: Encoder,
    claims: list[Claim],
    stream: ProgressStream,
    corroborate: bool,
    contradict: bool,
) -> None:
    """Fill each claim's `corroborated_by` when `corroborate`, and `contradicted_by` when
    `contradict`, from the claims of other sites closest to it.

    With a grounding checker in `models`, it decides support; otherwise the `verify` model does.
    Support is decided for every pair before any is asked about disagreeing: the checker and the
    `contradict` model share one local server, and alternating between them would load a model for
    each pair. Two claims either of which supports the other are not asked about disagreeing.
    """
    if not (corroborate or contradict):
        return
    pairs = neighbours(encoder, claims)
    if not pairs:
        return
    # Merged into it during the research already.
    agree = {(claim.id, other.id) for claim, other in pairs if other.id in claim.corroborated_by}
    if corroborate:
        asked = [(claim, other) for claim, other in pairs if (claim.id, other.id) not in agree]
        total = len(asked)
        stream.publish(
            VERIFY,
            f"checking {total} pairs of claims from different sites for support",
            0,
            total,
            series="support",
        )
        for done, (claim, other) in enumerate(asked, 1):
            if _supports(models, other, claim):
                claim.corroborated_by.append(other.id)
                agree.add((claim.id, other.id))
            stream.publish(
                VERIFY,
                f"checked {done} of {total} pairs for support",
                done,
                total,
                series="support",
            )
    if contradict:
        _contradictions(models["contradict"], pairs, agree, stream)
    stream.publish(
        VERIFY,
        f"{sum(bool(c.corroborated_by) for c in claims)} claims corroborated, "
        f"{sum(bool(c.contradicted_by) for c in claims)} contradicted",
        len(pairs),
        len(pairs),
    )


def _contradictions(
    model: GatewayModel,
    pairs: list[tuple[Claim, Claim]],
    agree: set[tuple[int, int]],
    stream: ProgressStream,
) -> None:
    """Fill `contradicted_by`, both ways, for the pairs not in `agree` either way round.

    Disagreeing is symmetric, so each pair is asked once. A claim is asked about all its pairs in
    one call, which cuts the calls about threefold, and the calls run side by side.
    """
    groups: dict[int, tuple[Claim, list[Claim]]] = {}
    seen: set[frozenset[int]] = set()
    for claim, other in pairs:
        key = frozenset((claim.id, other.id))
        if key in seen or (claim.id, other.id) in agree or (other.id, claim.id) in agree:
            continue
        seen.add(key)
        groups.setdefault(claim.id, (claim, []))[1].append(other)
    asked = list(groups.values())
    total = len(seen)
    stream.publish(
        VERIFY,
        f"comparing {total} pairs of claims for disagreement",
        0,
        total,
        series="disagreement",
    )
    done = 0
    with runtime.ContextThreadPoolExecutor(max_workers=runtime.max_threads()) as pool:
        found_all = pool.map(lambda group: _disagreeing(model, *group), asked)
        for (claim, others), found in zip(asked, found_all, strict=True):
            for other in found:
                claim.contradicted_by.append(other.id)
                other.contradicted_by.append(claim.id)
            done += len(others)
            stream.publish(
                VERIFY,
                f"compared {done} of {total} pairs for disagreement",
                done,
                total,
                series="disagreement",
            )


def neighbours(encoder: Encoder, claims: list[Claim]) -> list[tuple[Claim, Claim]]:
    """Each claim with up to `NEIGHBOURS` claims from other sites about the same thing."""
    if len(claims) < 2:
        return []
    vectors = encoder.encode([claim.text for claim in claims])
    similarity = cosine_similarity(vectors, vectors)
    pairs: list[tuple[Claim, Claim]] = []
    for i, claim in enumerate(claims):
        close = [
            int(j)
            for j in np.argsort(similarity[i])[::-1]
            if similarity[i][j] >= SAME_FACT and independent(claim, claims[j])
        ]
        pairs.extend((claim, claims[j]) for j in close[:NEIGHBOURS])
    return pairs


def _supports(models: dict[str, GatewayModel], other: Claim, claim: Claim) -> bool:
    if (checker := models.get("ground")) is not None:
        return grounded(checker, f"{other.text}\n{other.quote}", claim.text)
    try:
        verdict = predict(
            models["verify"], Verify, sentence=claim.text, evidence=quoted_claims([other])
        )
    except (AdapterParseError, ValidationError) as exc:
        logger.warning("could not read whether [%d] supports [%d]: %s", other.id, claim.id, exc)
        return False
    return bool(verdict.verdict == "supported")


def _disagreeing(model: GatewayModel, claim: Claim, others: list[Claim]) -> list[Claim]:
    """Those of `others` that disagree with `claim`; none when the answer cannot be read."""
    try:
        verdict = predict(
            model, Contradicts, claim=quoted_claims([claim]), others=quoted_claims(others)
        )
    except (AdapterParseError, ValidationError) as exc:
        logger.warning("could not read which claims disagree with [%d]: %s", claim.id, exc)
        return []
    # Only ids it was shown: a small model otherwise cites claims from its own numbering.
    return [other for other in others if other.id in verdict.disagree]
