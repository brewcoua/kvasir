"""Construction of the two upstream runners from settings.

Every language model role is set explicitly. The convenience initialiser
`CollaborativeStormLMConfigs.init()` hardcodes `api_base=None` and cannot be pointed at a gateway,
so it is never called.

See docs/fork-notes.md for the signatures these rely on.
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
from kvasir.storm.encoder import Encoder
from kvasir.storm.lm import GatewayModel
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


def _language_model(settings: Settings, role: str, tier: str, max_tokens: int) -> GatewayModel:
    """A model bound to the gateway.

    The name's first segment routes and is consumed by dspy; the rest reaches the gateway. See
    `kvasir.config._model`.
    """
    return GatewayModel(
        model=settings.model_fast if tier == "fast" else settings.model_strong,
        # Reported with the model's usage, so a run can be read as which stage spent what.
        role=role,
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
    and one a local sentence-transformer, and neither took the gateway. Both are one `dspy.Embedder`
    now, so the name routes exactly as a language model's does.
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
        getattr(lm_configs, f"set_{role}_lm")(_language_model(settings, role, tier, max_tokens))

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


def _costorm_lm_config(settings: Settings) -> CollaborativeStormLMConfigs:
    lm_config = CollaborativeStormLMConfigs()
    for role, (tier, max_tokens) in _COSTORM_ROLES.items():
        getattr(lm_config, f"set_{role}_lm")(_language_model(settings, role, tier, max_tokens))
    return lm_config


def build_costorm_runner(
    settings: Settings,
    topic: str,
    *,
    model_fast: str | None = None,
    model_strong: str | None = None,
) -> CoStormRunner:
    """Build a Co-STORM runner for `topic`. Call `warm_start()` on it before stepping."""
    settings = _with_model_overrides(settings, model_fast, model_strong)

    lm_config = _costorm_lm_config(settings)
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

    The models, encoder and retriever come from current settings rather than from the file, so a
    configuration change takes effect on the next turn of an existing session. The runner arguments
    are the exception: they are restored, since they shaped the conversation already in the file.
    """
    return CoStormRunner.from_dict(
        state,
        lm_config=_costorm_lm_config(settings),
        encoder=_encoder(settings),
        rm=_retriever(settings, settings.search_top_k),
    )


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
