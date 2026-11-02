"""Which question to search for next, judged on what the research already holds.

The default order is breadth first: least tried, then shallowest. The frontier orders questions
by a worth score computed in code instead, and stops searching a branch once it stops adding
anything. A branch is a first question and its follow-ups. A new claim that repeats one already
held is merged into it: it adds a source when it comes from another site, and nothing otherwise.

The weights are not tuned. They are a first guess, to be kept or changed on evaluation.
"""

from __future__ import annotations

import numpy as np

from kvasir.methods.ledger.questions import Question
from kvasir.research.claims import Claim
from kvasir.research.corroborate import domain
from kvasir.storm.encoder import Encoder, cosine_similarity

# Above this cosine similarity, a new claim repeats one already held.
DUPLICATE_CLAIM = 0.92
# Searches in a row on one branch that add no claim and no source before the branch is closed.
BRANCH_STALL = 2
# What lowers a question's worth: each level below the first questions, and each query tried.
DEPTH_PENALTY = 0.15
ATTEMPT_PENALTY = 0.25
# What raises it: a question with part of an answer, or with sources that disagree.
GAP_BONUS = 0.2


class Frontier:
    """The worth of each question, the claims merged so far, and the branches closed."""

    def __init__(self, encoder: Encoder, brief: str, claims: list[Claim]) -> None:
        self.encoder = encoder
        self.brief = encoder.encode([brief])[0]
        # The run's claims, read as they grow; their vectors are encoded once each.
        self.claims = claims
        self.claim_vectors: list[np.ndarray] = []
        self.question_vectors: dict[int, np.ndarray] = {}
        self.stalled: dict[int, int] = {}
        self.closed: set[int] = set()

    def pick(self, questions: list[Question], max_attempts: int) -> Question | None:
        """The question worth most of those not settled, outside a closed branch."""
        root_of = roots(questions)
        waiting = [
            q
            for q in questions
            if q.status in ("open", "weak", "contradicted")
            and q.attempts < max_attempts
            and root_of[q.id] not in self.closed
        ]
        if not waiting:
            return None
        return max(waiting, key=lambda q: (self.worth(q), -q.id))

    def worth(self, question: Question) -> float:
        """Closeness to the brief, plus distance from every claim held, plus any gap, less depth
        and the queries already tried."""
        vector = self._question(question)
        relevance = float(cosine_similarity(vector, self.brief)[0][0])
        held = self.held()
        novelty = 1.0 - float(cosine_similarity(vector, held).max()) if len(held) else 1.0
        gap = GAP_BONUS if question.status in ("weak", "contradicted") else 0.0
        return (
            relevance
            + novelty
            + gap
            - DEPTH_PENALTY * question.depth
            - ATTEMPT_PENALTY * question.attempts
        )

    def searched(self, questions: list[Question], question: Question, new: list[Claim]) -> bool:
        """Merge the claims a search for `question` found, already added to the run's claims, and
        close its branch if they added nothing. True when the branch was closed."""
        root = roots(questions)[question.id]
        if self.merge(new):
            self.stalled[root] = 0
            return False
        self.stalled[root] = self.stalled.get(root, 0) + 1
        if self.stalled[root] < BRANCH_STALL:
            return False
        self.closed.add(root)
        return True

    def merge(self, new: list[Claim]) -> int:
        """How many of `new`, the last claims of the run, added a claim or a source. A repeat from
        another site is recorded as corroborating the claim it repeats."""
        vectors = self.held()
        before = len(self.claims) - len(new)
        # Row i is claim i, so the closest row names the claim repeated.
        held = list(vectors[:before])
        added = 0
        for claim, vector in zip(new, vectors[before:], strict=True):
            if held:
                scores = cosine_similarity(vector, np.array(held))[0]
                best = int(np.argmax(scores))
                if scores[best] >= DUPLICATE_CLAIM:
                    kept = self.claims[best]
                    if domain(kept.url) != domain(claim.url):
                        kept.corroborated_by.append(claim.id)
                        added += 1
                    held.append(vector)
                    continue
            held.append(vector)
            added += 1
        return added

    def held(self) -> np.ndarray:
        """Every claim's vector, encoding only those not encoded before."""
        missing = self.claims[len(self.claim_vectors) :]
        if missing:
            self.claim_vectors.extend(self.encoder.encode([c.text for c in missing]))
        return np.array(self.claim_vectors)

    def _question(self, question: Question) -> np.ndarray:
        if question.id not in self.question_vectors:
            self.question_vectors[question.id] = self.encoder.encode([question.text])[0]
        return self.question_vectors[question.id]


def roots(questions: list[Question]) -> dict[int, int]:
    """Each question's first question, the one its follow-ups descend from."""
    parents = {q.id: q.parent for q in questions}
    of = {}
    for q in questions:
        root = q.id
        while (parent := parents.get(root)) is not None:
            root = parent
        of[q.id] = root
    return of
