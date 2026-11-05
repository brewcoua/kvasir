"""Construction of the STORM runner and the research models from settings.

Every language model role is set explicitly. See docs/fork-notes.md for the signatures these rely
on.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from kvasir.config import Settings, SourceSettings
from kvasir.research.corroborate import domain
from kvasir.research.read import answer_tokens
from kvasir.research.sources import Sources, SourceStore
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel
from kvasir.storm.rm import SearXNG
from kvasir.storm.storm_wiki.engine import (
    STORMWikiLMConfigs,
    STORMWikiRunner,
    STORMWikiRunnerArguments,
)

# Upstream's own examples pick a token budget per role, and the numbers below are theirs. They are
# not arbitrary: a role that emits a whole polished article needs far more room than one that
# decides who speaks next. Which tier serves a role is our choice, and follows the rule that the
# fast model simulates conversation, asks questions and polishes, while the strong model produces
# outlines and article text. Upstream puts polishing on the strong model; the deployment this
# serves is billed per token through a gateway and prefers the cheaper tier there.
_STORM_ROLES = {
    "conv_simulator": ("fast", 500),
    "question_asker": ("fast", 500),
    "outline_gen": ("strong", 400),
    "article_gen": ("strong", 700),
    "article_polish": ("fast", 4000),
}

# Room for a handful of follow-up questions, which is the largest of the three things Open WebUI
# asks a task call for. A title needs a fraction of it.
TASK_MAX_TOKENS = 1000
# A client asking for a chat title has its own short timeout and has given up long before the
# default budget runs out, so retrying past this only leaves a thread spinning on a dead gateway.
TASK_RETRY_SECONDS = 30.0

# A bound on a section's prompt: one run's came to 2,200 to 2,700 tokens, with 15 claims and the
# start of each earlier section.
WRITE_PROMPT_TOKENS = 4000

# A checker answers Yes or No.
CHECKER_MAX_TOKENS = 10


# The deep research pipeline's steps, on three tiers. Extraction and planning are many small calls
# and go to the fast model; the brief, the outline and the prose a reader sees go to the strong
# one; the judgements go to the reason model, which thinks before it answers. Its budgets are about
# three times what the answer needs, because thinking counts against max_tokens. Intake and notes
# return lists of claims, which need more room than a plan's handful of queries.
_RESEARCH_ROLES = {
    "clarify": ("fast", 500),
    # The strong model thinks before it answers too; a brief alone once used 876 of 1000.
    "brief": ("strong", 2500),
    "decompose": ("reason", 2000),
    "plan": ("fast", 500),
    "check": ("reason", 2500),
    "criteria": ("reason", 3000),
    "entities": ("reason", 2000),
    "assess": ("fast", 600),
    "judge": ("reason", 2500),
    # A thinking strong model spends some 700 tokens reasoning before it answers, within these.
    "outline": ("strong", 2500),
    "tldr": ("strong", 2000),
    "verify": ("fast", 600),
    "contradict": ("fast", 600),
    # The reason model's final say on a sentence the checker and the fast model reject or
    # disagree on.
    "escalate": ("reason", 2500),
}


def _language_model(settings: Settings, role: str, tier: str, max_tokens: int) -> GatewayModel:
    """A model bound to the endpoint of its provider.

    The name's first segment routes and is consumed by dspy; the rest reaches the provider. See
    `Settings.resolve`.
    """
    resolved = settings.resolve(
        {
            "fast": settings.model.fast,
            "strong": settings.model.strong,
            "reason": settings.model.reason or settings.model.strong,
        }[tier]
    )
    effort = settings.model.reason_effort if tier == "reason" else settings.model.reasoning_effort
    return GatewayModel(
        model=resolved.model,
        # Reported with the model's usage, so a run can be read as which stage spent what.
        role=role,
        api_key=resolved.api_key,
        api_base=resolved.api_base,
        max_tokens=max_tokens,
        # Upstream's examples use these for STORM. The multi-perspective research depends on the
        # simulated conversations diverging, which temperature 0 would suppress.
        temperature=1.0,
        top_p=0.9,
        # In the body rather than as litellm's own `reasoning_effort`, which litellm refuses for a
        # model it has no entry for, such as anything served by Ollama.
        **({"extra_body": {"reasoning_effort": effort}} if effort else {}),
    )


def build_task_model(settings: Settings) -> GatewayModel:
    """The fast model, for a short auxiliary call that belongs to no run.

    Nothing binds a usage sink around it, so what it spends is reported nowhere. That is deliberate:
    a client's housekeeping is not part of any run's accounting.
    """
    return _language_model(settings, role="task", tier="fast", max_tokens=TASK_MAX_TOKENS)


def build_research_models(
    settings: Settings,
    model_fast: str | None = None,
    model_strong: str | None = None,
    model_reason: str | None = None,
) -> dict[str, GatewayModel]:
    """One model per deep research step, keyed by step."""
    settings = _with_model_overrides(settings, model_fast, model_strong, model_reason)
    models = {
        role: _language_model(settings, role, tier, max_tokens)
        for role, (tier, max_tokens) in _RESEARCH_ROLES.items()
    }
    # An intake call reads as much of a page as the context holds, so its answer grows with it.
    models["intake"] = _language_model(
        settings, "intake", "fast", answer_tokens(settings.model.context_tokens)
    )
    # The report is what a reader sees, so a section gets all the context its prompt leaves. A
    # thinking model's reasoning shares this with the answer; no provider here caps it apart.
    models["write"] = _language_model(
        settings, "write", "strong", settings.model.context_tokens - WRITE_PROMPT_TOKENS
    )
    return models


def build_checker(settings: Settings) -> GatewayModel | None:
    """The grounding checker, if one is configured. See `kvasir.research.ground`.

    At temperature 0, unlike every other role: a verdict that changes between two calls on the
    same quote is no check. No reasoning effort is sent, since a checker is not a thinking model.
    """
    if settings.model.checker is None:
        return None
    resolved = settings.resolve(settings.model.checker)
    return GatewayModel(
        model=resolved.model,
        role="ground",
        api_key=resolved.api_key,
        api_base=resolved.api_base,
        max_tokens=CHECKER_MAX_TOKENS,
        temperature=0.0,
    )


_sources: tuple[tuple[Path, SourceSettings], Sources] | None = None
_sources_lock = threading.Lock()


def build_sources(settings: Settings) -> Sources | None:
    """The source classifier, built once per process for these settings, or None when
    `[sources] enabled` is false."""
    global _sources
    if not settings.sources.enabled:
        return None
    key = (settings.sources_db, settings.sources)
    with _sources_lock:
        if _sources is None or _sources[0] != key:
            sources = Sources(settings.sources, SourceStore(settings.sources_db), settings.data_dir)
            _sources = (key, sources)
        return _sources[1]


def build_research_tools(settings: Settings) -> tuple[Encoder, SearXNG]:
    """The encoder and the search engine, for the deep research pipeline. It caps results itself."""
    return _encoder(settings), _retriever(settings, settings.search.top_k)


def _encoder(settings: Settings) -> Encoder:
    """The embedding model, which ranks snippets against each section's query.

    Upstream's encoder took no configured provider. This one is a `dspy.Embedder`, so the name
    routes exactly as a language model's does.
    """
    resolved = settings.resolve(settings.model.embedding)
    return Encoder(model=resolved.model, api_key=resolved.api_key, api_base=resolved.api_base)


def _retriever(settings: Settings, k: int) -> SearXNG:
    """SearXNG reads no environment variable, so the URL is passed in.

    The instance must serve the JSON output format. One serving HTML only returns an empty result
    set rather than failing, which looks like a topic with no sources.
    """
    sources = build_sources(settings)
    if sources is None:
        return SearXNG(searxng_api_url=settings.search.url, k=k)

    def is_valid_source(url: str) -> bool:
        # STORM reads what this passes; a URL that cannot be read is not worth a query's results.
        try:
            host = domain(url)
        except ValueError:
            return False
        return sources.classify([host])[host].tier != "avoid"

    return SearXNG(searxng_api_url=settings.search.url, k=k, is_valid_source=is_valid_source)


def build_storm_runner(
    settings: Settings,
    output_dir: Path,
    *,
    search_top_k: int | None = None,
    max_conv_turn: int | None = None,
    max_perspective: int | None = None,
    model_fast: str | None = None,
    model_strong: str | None = None,
) -> STORMWikiRunner:
    """Build a STORM runner writing under `output_dir`.

    The per-request overrides fall back to the configured defaults when omitted.
    """
    settings = _with_model_overrides(settings, model_fast, model_strong)

    lm_configs = STORMWikiLMConfigs()
    for role, (tier, max_tokens) in _STORM_ROLES.items():
        getattr(lm_configs, f"set_{role}_lm")(_language_model(settings, role, tier, max_tokens))

    top_k = search_top_k if search_top_k is not None else settings.search.top_k
    arguments = STORMWikiRunnerArguments(
        output_dir=str(output_dir),
        max_conv_turn=max_conv_turn if max_conv_turn is not None else settings.storm.max_conv_turn,
        max_perspective=(
            max_perspective if max_perspective is not None else settings.storm.max_perspective
        ),
        search_top_k=top_k,
        max_thread_num=settings.max_threads,
    )
    return STORMWikiRunner(arguments, lm_configs, _retriever(settings, top_k), _encoder(settings))


def _with_model_overrides(
    settings: Settings,
    model_fast: str | None,
    model_strong: str | None,
    model_reason: str | None = None,
) -> Settings:
    if model_fast is None and model_strong is None and model_reason is None:
        return settings
    return replace(
        settings,
        model=replace(
            settings.model,
            fast=model_fast or settings.model.fast,
            strong=model_strong or settings.model.strong,
            reason=model_reason or settings.model.reason,
        ),
    )


def check_model_overrides(
    settings: Settings,
    model_fast: str | None,
    model_strong: str | None,
    model_reason: str | None = None,
) -> None:
    """Raise `ConfigError` if a per-request model names no configured provider.

    Called by the routes before a run starts, so a bad override is refused rather than failing
    minutes in, at the first model call.
    """
    for ref in (model_fast, model_strong, model_reason):
        if ref is not None:
            settings.resolve(ref)
