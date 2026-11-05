"""No network. Building a runner makes no request; only running one does."""

from dataclasses import replace

import pytest

from kvasir.config import ConfigError, Provider, Settings
from kvasir.runners import (
    WRITE_PROMPT_TOKENS,
    build_research_models,
    build_storm_runner,
    check_model_overrides,
)

ENV = {
    "KVASIR_PROVIDER_GATEWAY_API_KEY": "key",
    "KVASIR_PROVIDER_GATEWAY_API_BASE": "https://gateway.example/v1",
    "KVASIR_TOKEN": "token",
}
STORED = {
    "model.fast": "gateway/ollama/fast:cloud",
    "model.strong": "gateway/ollama/strong:cloud",
    "model.embedding": "gateway/ollama/embed:cloud",
    "search.url": "http://searxng.example",
}

STORM_ROLES = (
    "conv_simulator_lm",
    "question_asker_lm",
    "outline_gen_lm",
    "article_gen_lm",
    "article_polish_lm",
)


@pytest.fixture
def settings():
    return Settings.load(ENV, STORED)


@pytest.fixture
def storm(settings, tmp_path):
    return build_storm_runner(settings, tmp_path)


def _models(configs, roles):
    return [getattr(configs, role) for role in roles]


@pytest.mark.parametrize("role", STORM_ROLES)
def test_every_storm_role_reaches_the_gateway(storm, role):
    model = getattr(storm.lm_configs, role)

    assert model is not None
    assert model.kwargs["api_base"] == "https://gateway.example/v1"


def test_model_names_are_not_rewritten(storm):
    used = {model.model for model in _models(storm.lm_configs, STORM_ROLES)}

    assert used == {"openai/ollama/fast:cloud", "openai/ollama/strong:cloud"}


def test_storm_writes_where_it_is_told(storm, tmp_path):
    assert storm.args.output_dir == str(tmp_path)


def test_storm_defaults_come_from_settings(storm):
    assert storm.args.max_conv_turn == 3
    assert storm.args.max_perspective == 3
    assert storm.args.search_top_k == 3


def test_storm_per_request_overrides_apply(settings, tmp_path):
    runner = build_storm_runner(
        settings,
        tmp_path,
        search_top_k=7,
        max_conv_turn=1,
        max_perspective=2,
        model_strong="gateway/ollama/other:cloud",
    )

    assert runner.args.search_top_k == 7
    assert runner.args.max_conv_turn == 1
    assert runner.args.max_perspective == 2
    assert runner.retriever.rm.k == 7
    assert runner.lm_configs.article_gen_lm.model == "openai/ollama/other:cloud"
    assert runner.lm_configs.conv_simulator_lm.model == "openai/ollama/fast:cloud"


def test_overrides_do_not_mutate_the_shared_settings(settings, tmp_path):
    build_storm_runner(settings, tmp_path, model_fast="gateway/ollama/other:cloud")

    assert settings.model.fast == "gateway/ollama/fast:cloud"


def test_the_outline_and_the_prose_are_written_by_the_strong_model(settings):
    models = build_research_models(settings)

    for role in ("outline", "write", "tldr"):
        assert models[role].model == "openai/ollama/strong:cloud", role
    assert models["intake"].model == "openai/ollama/fast:cloud"


def test_a_section_gets_all_the_context_its_prompt_leaves(settings):
    models = build_research_models(settings)

    budget = settings.model.context_tokens - WRITE_PROMPT_TOKENS
    assert models["write"].kwargs["max_tokens"] == budget


REASON_ROLES = ("decompose", "check", "criteria", "entities", "judge", "escalate")


def test_the_judgements_are_made_by_the_reason_model():
    settings = Settings.load(ENV, STORED | {"model.reason": "gateway/ollama/reason:cloud"})

    models = build_research_models(settings)

    for role in REASON_ROLES:
        assert models[role].model == "openai/ollama/reason:cloud", role
    assert models["write"].model == "openai/ollama/strong:cloud"
    assert models["assess"].model == "openai/ollama/fast:cloud"


def test_the_reason_model_falls_back_to_the_strong_one(settings):
    models = build_research_models(settings)

    for role in REASON_ROLES:
        assert models[role].model == "openai/ollama/strong:cloud", role


def test_a_request_may_override_the_reason_model(settings):
    models = build_research_models(settings, model_reason="gateway/ollama/other:cloud")

    assert models["judge"].model == "openai/ollama/other:cloud"
    assert models["write"].model == "openai/ollama/strong:cloud"


def test_the_reason_tier_gets_its_own_effort():
    settings = Settings.load(ENV, STORED | {"model.reasoning_effort": "none"})

    models = build_research_models(settings)

    for role in REASON_ROLES:
        assert models[role].kwargs["extra_body"] == {"reasoning_effort": "medium"}, role
    for role in ("plan", "write", "intake"):
        assert models[role].kwargs["extra_body"] == {"reasoning_effort": "none"}, role


def test_a_model_on_another_provider_gets_that_providers_endpoint_and_key():
    settings = Settings.load(
        ENV
        | {
            "KVASIR_PROVIDER_OTHER_API_BASE": "https://other.example/v1",
            "KVASIR_PROVIDER_OTHER_API_KEY": "other-key",
        },
        STORED | {"model.strong": "other/big"},
    )

    models = build_research_models(settings)

    assert models["write"].model == "openai/big"
    assert models["write"].kwargs["api_base"] == "https://other.example/v1"
    assert models["write"].kwargs["api_key"] == "other-key"
    assert models["plan"].kwargs["api_base"] == "https://gateway.example/v1"
    assert models["plan"].kwargs["api_key"] == "key"


def test_an_override_on_another_provider_switches_provider(settings):
    settings = replace(
        settings,
        providers=settings.providers
        | {"other": Provider(api_base="https://other.example/v1", api_key="other-key")},
    )

    models = build_research_models(settings, model_strong="other/x")

    assert models["brief"].model == "openai/x"
    assert models["brief"].kwargs["api_base"] == "https://other.example/v1"
    assert models["brief"].kwargs["api_key"] == "other-key"
    assert models["plan"].kwargs["api_base"] == "https://gateway.example/v1"


def test_an_override_naming_an_unknown_provider_is_refused(settings):
    with pytest.raises(ConfigError, match="unknown provider 'nope'"):
        check_model_overrides(settings, None, "nope/x")
    with pytest.raises(ConfigError, match="must name a provider"):
        check_model_overrides(settings, "fast", None)
    with pytest.raises(ConfigError, match="unknown provider 'nope'"):
        check_model_overrides(settings, None, None, "nope/x")
    check_model_overrides(settings, "gateway/x", None, "gateway/y")


def test_reasoning_effort_is_not_sent_unless_set(storm):
    for role in STORM_ROLES:
        assert "extra_body" not in getattr(storm.lm_configs, role).kwargs, role


def test_reasoning_effort_reaches_every_role_in_the_body(tmp_path):
    settings = Settings.load(ENV, STORED | {"model.reasoning_effort": "none"})
    storm = build_storm_runner(settings, tmp_path)

    for model in _models(storm.lm_configs, STORM_ROLES):
        assert model.kwargs["extra_body"] == {"reasoning_effort": "none"}
