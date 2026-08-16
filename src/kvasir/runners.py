"""Construction of the two upstream runners from settings.

Every language model role is set explicitly. The convenience initialiser
`CollaborativeStormLMConfigs.init()` hardcodes `api_base=None` and cannot be pointed at a gateway,
so it is never called. The retriever is always passed explicitly too, because `CoStormRunner`
defaults to `BingSearch`, which needs a paid key.

See docs/upstream-notes.md for the signatures these rely on.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from kvasir.config import Settings
from kvasir.storm.collaborative_storm.engine import (
    CollaborativeStormLMConfigs,
    CoStormRunner,
    RunnerArgument,
)
from kvasir.storm.dataclass import ConversationTurn, KnowledgeBase
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import LitellmModel
from kvasir.storm.logging_wrapper import LoggingWrapper
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

_COSTORM_ROLES = {
    "question_answering": ("strong", 1000),
    "discourse_manage": ("fast", 500),
    "utterance_polishing": ("fast", 2000),
    "warmstart_outline_gen": ("strong", 500),
    "question_asking": ("fast", 300),
    "knowledge_base": ("strong", 1000),
}


def _language_model(settings: Settings, tier: str, max_tokens: int) -> LitellmModel:
    """A model bound to the gateway.

    `LitellmModel` merges its kwargs into `litellm.completion()`, so `api_base` arrives intact.
    `OpenAIModel` is deprecated and accepts no `api_base`, which is why it is not used.
    """
    return LitellmModel(
        model=settings.model_fast if tier == "fast" else settings.model_strong,
        api_key=settings.openai_api_key,
        api_base=settings.openai_api_base,
        max_tokens=max_tokens,
        # Upstream's examples use these for STORM. The multi-perspective research depends on the
        # simulated conversations diverging, which temperature 0 would suppress.
        temperature=1.0,
        top_p=0.9,
    )


def _encoder(settings: Settings) -> Encoder:
    """The embedding model, for both modes.

    Co-STORM embeds to build its mind map; STORM embeds to rank snippets against each section's
    query. Upstream used two unrelated encoders for those, one hardcoded to `text-embedding-3-small`
    and one a local sentence-transformer, and neither took the gateway.
    """
    return Encoder(
        model=settings.embedding_model,
        api_key=settings.openai_api_key,
        api_base=settings.openai_api_base,
    )


def _retriever(settings: Settings, k: int) -> SearXNG:
    """SearXNG reads no environment variable, so the URL is passed in.

    The instance must serve the JSON output format. One serving HTML only returns an empty result
    set rather than failing, which looks like a topic with no sources.
    """
    return SearXNG(searxng_api_url=settings.searxng_url, k=k)


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
        getattr(lm_configs, f"set_{role}_lm")(_language_model(settings, tier, max_tokens))

    top_k = search_top_k if search_top_k is not None else settings.search_top_k
    arguments = STORMWikiRunnerArguments(
        output_dir=str(output_dir),
        max_conv_turn=max_conv_turn if max_conv_turn is not None else settings.max_conv_turn,
        max_perspective=(
            max_perspective if max_perspective is not None else settings.max_perspective
        ),
        search_top_k=top_k,
        max_thread_num=settings.max_threads,
    )
    return STORMWikiRunner(arguments, lm_configs, _retriever(settings, top_k), _encoder(settings))


def build_costorm_runner(
    settings: Settings,
    topic: str,
    *,
    model_fast: str | None = None,
    model_strong: str | None = None,
) -> CoStormRunner:
    """Build a Co-STORM runner for `topic`. Call `warm_start()` on it before stepping."""
    settings = _with_model_overrides(settings, model_fast, model_strong)

    lm_config = CollaborativeStormLMConfigs()
    for role, (tier, max_tokens) in _COSTORM_ROLES.items():
        getattr(lm_config, f"set_{role}_lm")(_language_model(settings, tier, max_tokens))

    runner = CoStormRunner(
        lm_config=lm_config,
        runner_argument=RunnerArgument(
            topic=topic,
            retrieve_top_k=settings.search_top_k,
            max_search_thread=settings.max_threads,
            max_thread_num=settings.max_threads,
        ),
        logging_wrapper=LoggingWrapper(lm_config),
        encoder=_encoder(settings),
        rm=_retriever(settings, settings.search_top_k),
    )
    return runner


def load_costorm_runner(settings: Settings, state: dict[str, Any]) -> CoStormRunner:
    """Restore a Co-STORM runner from `to_dict()` output.

    `CoStormRunner.from_dict` is deliberately not used. It calls
    `CollaborativeStormLMConfigs.init()`, which hardcodes `api_base=None` against
    `gpt-4o-2024-05-13`, so a restored session would talk to api.openai.com rather than the
    gateway. It also builds no retriever, falling back to `BingSearch`. Its own source carries a
    FIXME about discarding the serialised model configuration.

    So the runner is built correctly first and the conversation state is restored onto it. That is
    what `from_dict` does either way; only the parts it gets wrong are replaced.

    The runner arguments come from current settings rather than from the file, so a configuration
    change takes effect on the next turn of an existing session.
    """
    runner = build_costorm_runner(settings, state["runner_argument"]["topic"])

    runner.conversation_history = [
        ConversationTurn.from_dict(turn) for turn in state["conversation_history"]
    ]
    runner.warmstart_conv_archive = [
        ConversationTurn.from_dict(turn) for turn in state.get("warmstart_conv_archive", [])
    ]
    runner.discourse_manager.deserialize_experts(state["experts"])
    runner.knowledge_base = KnowledgeBase.from_dict(
        data=state["knowledge_base"],
        knowledge_base_lm=runner.lm_config.knowledge_base_lm,
        node_expansion_trigger_count=runner.runner_argument.node_expansion_trigger_count,
        encoder=runner.encoder,
    )
    return runner


def _with_model_overrides(
    settings: Settings, model_fast: str | None, model_strong: str | None
) -> Settings:
    if model_fast is None and model_strong is None:
        return settings
    return replace(
        settings,
        model_fast=model_fast or settings.model_fast,
        model_strong=model_strong or settings.model_strong,
    )
