"""Whether a document supports a claim, asked of a small grounding checker.

Tier 2 of verification: cheaper than asking the model to judge, and on the LLM-AggreFact benchmark
as accurate as a far larger model prompted to. MiniCheck models, such as `bespoke-minicheck` on
Ollama, read this exact prompt and answer Yes or No. A claim the checker supports is attributed to
its quote, not proven true; corroboration is what checks it against other sources.
"""

from __future__ import annotations

from kvasir.storm.lm import GatewayModel

PROMPT = "Document: {document}\nClaim: {claim}"


def grounded(checker: GatewayModel, document: str, claim: str) -> bool:
    """Whether `document` supports `claim`. An answer that is not Yes counts as No."""
    replies = checker(prompt=PROMPT.format(document=document, claim=claim))
    return bool(replies) and str(replies[0]).strip().lower().startswith("yes")
