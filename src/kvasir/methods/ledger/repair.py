"""Searching again for what verification removed, and writing again what the search supports."""

from __future__ import annotations

from kvasir.methods.ledger.loop import Judge, Research
from kvasir.methods.ledger.questions import Question
from kvasir.research import verify, write
from kvasir.research.claims import Claim
from kvasir.runs.progress import VERIFY, ProgressStream
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel

# Sentences verification removed that are searched for again, at most, and the searches that may
# take. One pass: a rewritten section's own removals stand.
REPAIR_SENTENCES = 4
REPAIR_SEARCHES = 4


def repair(
    models: dict[str, GatewayModel],
    encoder: Encoder,
    brief: str,
    titles: list[str],
    claims: list[Claim],
    checked: verify.Verified,
    research: Research,
    judge: Judge,
    stream: ProgressStream,
) -> verify.Verified:
    """Search for what verification removed, and write again the sections the search supports.

    A removed sentence is often true, cited to a claim that does not say it. Each becomes a
    question; a section whose question the search answers is written again with the claims that
    answer it, and checked again; what that check did to its sentences replaces what the first
    check did.
    """
    picked = [r for r in checked.removed if r.section in titles][:REPAIR_SENTENCES]
    if not picked:
        return checked
    questions = [
        Question(id=n, text=verify.unmarked(r.sentence), missing=r.reason, judged_at=len(claims))
        for n, r in enumerate(picked, 1)
    ]
    stream.publish(VERIFY, f"searching again for {len(picked)} removed sentences")
    research(questions, REPAIR_SEARCHES, judge)
    found: dict[str, list[int]] = {}
    for sentence, question in zip(picked, questions, strict=True):
        if question.status == "answered":
            found.setdefault(sentence.section, []).extend(question.claims)
    stream.publish(VERIFY, f"found support for {len(found)} of the sections they came from")

    by_id = {claim.id: claim for claim in claims}
    sections = list(checked.sections)
    removed, revised, unverified = checked.removed, checked.revised, checked.unverified
    for title, ids in found.items():
        index = next(i for i, (t, _) in enumerate(sections) if t == title)
        stream.publish(VERIFY, f"writing {title} again")
        text = write.section(
            models["write"],
            encoder,
            brief,
            titles,
            title,
            [s for s in sections[:index] if s[0] in titles],
            claims,
            ids,
        )
        again = verify.report(models, [(title, text)], by_id, stream)
        sections[index] = again.sections[0]
        removed = [r for r in removed if r.section != title] + again.removed
        revised = [r for r in revised if r.section != title] + again.revised
        unverified = [u for u in unverified if u.section != title] + again.unverified
    return verify.Verified(sections, removed, revised, unverified)
