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

from kvasir.research.calls import predict
from kvasir.research.claims import Claim, quoted_claims
from kvasir.research.ground import grounded
from kvasir.research.signatures import Contradicts, Verify
from kvasir.runs.progress import VERIFY, ProgressStream
from kvasir.storm.encoder import Encoder, cosine_similarity
from kvasir.storm.lm import GatewayModel

# Claims from other sites each claim is compared with, at most.
NEIGHBOURS = 3
# Below this cosine similarity, two claims are taken to be about different things.
SAME_FACT = 0.8

logger = logging.getLogger(__name__)


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.")


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

    A claim another supports is not also checked for contradicting it. With a grounding checker in
    `models`, it decides support; otherwise the `verify` model does.
    """
    if not (corroborate or contradict):
        return
    pairs = neighbours(encoder, claims)
    if not pairs:
        return
    total = len(pairs)
    stream.publish(VERIFY, f"comparing {total} pairs of claims from different sites", 0, total)
    compared: set[tuple[int, int]] = set()
    for done, (claim, other) in enumerate(pairs, 1):
        if corroborate and _supports(models, other, claim):
            claim.corroborated_by.append(other.id)
        elif contradict and (other.id, claim.id) not in compared:
            # Disagreeing is symmetric, so each pair is asked once.
            compared.add((claim.id, other.id))
            if _contradicts(models["contradict"], claim, other):
                claim.contradicted_by.append(other.id)
                other.contradicted_by.append(claim.id)
        stream.publish(VERIFY, f"compared {done} of {total} pairs", done, total)
    stream.publish(
        VERIFY,
        f"{sum(bool(c.corroborated_by) for c in claims)} claims corroborated, "
        f"{sum(bool(c.contradicted_by) for c in claims)} contradicted",
        total,
        total,
    )


def neighbours(encoder: Encoder, claims: list[Claim]) -> list[tuple[Claim, Claim]]:
    """Each claim with up to `NEIGHBOURS` claims from other sites about the same thing."""
    if len(claims) < 2:
        return []
    vectors = encoder.encode([claim.text for claim in claims])
    similarity = cosine_similarity(vectors, vectors)
    sites = [domain(claim.url) for claim in claims]
    pairs: list[tuple[Claim, Claim]] = []
    for i, claim in enumerate(claims):
        close = [
            int(j)
            for j in np.argsort(similarity[i])[::-1]
            if sites[j] != sites[i] and similarity[i][j] >= SAME_FACT
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
    except AdapterParseError as exc:
        logger.warning("could not read whether [%d] supports [%d]: %s", other.id, claim.id, exc)
        return False
    return bool(verdict.supported)


def _contradicts(model: GatewayModel, claim: Claim, other: Claim) -> bool:
    try:
        verdict = predict(
            model, Contradicts, first=quoted_claims([claim]), second=quoted_claims([other])
        )
    except AdapterParseError as exc:
        logger.warning("could not read whether [%d] and [%d] disagree: %s", claim.id, other.id, exc)
        return False
    return bool(verdict.disagree)
