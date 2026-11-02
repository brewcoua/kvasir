"""No network. Building a runner makes no request; only running one does."""

import json
from dataclasses import replace

import pytest

from kvasir.config import ConfigError, Provider, Settings
from kvasir.runners import (
    build_costorm_runner,
    build_research_models,
    build_storm_runner,
    check_model_overrides,
    load_costorm_runner,
)

MINIMAL = {
    "KVASIR_PROVIDER_GATEWAY_API_KEY": "key",
    "KVASIR_PROVIDER_GATEWAY_API_BASE": "https://gateway.example/v1",
    "KVASIR_MODEL_FAST": "gateway/ollama/fast:cloud",
    "KVASIR_MODEL_STRONG": "gateway/ollama/strong:cloud",
    "KVASIR_SEARCH_URL": "http://searxng.example",
    "KVASIR_MODEL_EMBEDDING": "gateway/ollama/embed:cloud",
}

STORM_ROLES = (
    "conv_simulator_lm",
    "question_asker_lm",
    "outline_gen_lm",
    "article_gen_lm",
    "article_polish_lm",
)

COSTORM_ROLES = (
    "question_answering_lm",
    "discourse_manage_lm",
    "utterance_polishing_lm",
    "warmstart_outline_gen_lm",
    "question_asking_lm",
    "knowledge_base_lm",
)


@pytest.fixture
def settings():
    return Settings.from_env(MINIMAL)


@pytest.fixture
def storm(settings, tmp_path):
    return build_storm_runner(settings, tmp_path)


@pytest.fixture
def costorm(settings):
    return build_costorm_runner(settings, "a narrow topic")


def _models(configs, roles):
    return [getattr(configs, role) for role in roles]


@pytest.mark.parametrize("role", STORM_ROLES)
def test_every_storm_role_reaches_the_gateway(storm, role):
    model = getattr(storm.lm_configs, role)

    assert model is not None
    assert model.kwargs["api_base"] == "https://gateway.example/v1"


@pytest.mark.parametrize("role", COSTORM_ROLES)
def test_every_costorm_role_reaches_the_gateway(costorm, role):
    model = getattr(costorm.lm_config, role)

    assert model is not None
    assert model.kwargs["api_base"] == "https://gateway.example/v1"


def test_model_names_are_not_rewritten(storm, costorm):
    used = {model.model for model in _models(storm.lm_configs, STORM_ROLES)}
    used |= {model.model for model in _models(costorm.lm_config, COSTORM_ROLES)}

    assert used == {"openai/ollama/fast:cloud", "openai/ollama/strong:cloud"}


def test_both_tiers_are_actually_used(storm, costorm):
    for configs, roles in ((storm.lm_configs, STORM_ROLES), (costorm.lm_config, COSTORM_ROLES)):
        tiers = {model.model for model in _models(configs, roles)}
        assert len(tiers) == 2, f"expected both tiers in {roles}"


def test_costorm_uses_searxng_rather_than_the_paid_default(costorm):
    # Upstream fell back to BingSearch when rm was omitted, which needs a paid key.
    assert type(costorm.rm).__name__ == "SearXNG"
    assert costorm.rm.searxng_api_url == "http://searxng.example"


def test_costorm_encoder_uses_the_configured_embedding_model(costorm):
    # Encoder hardcodes text-embedding-3-small, which a prefix-routing gateway will not serve.
    assert costorm.encoder.embedding_model_name == "openai/ollama/embed:cloud"


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


def test_a_model_on_another_provider_gets_that_providers_endpoint_and_key():
    settings = Settings.from_env(
        MINIMAL
        | {
            "KVASIR_PROVIDER_OTHER_API_BASE": "https://other.example/v1",
            "KVASIR_PROVIDER_OTHER_API_KEY": "other-key",
            "KVASIR_MODEL_STRONG": "other/big",
        }
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
    check_model_overrides(settings, "gateway/x", None)


def test_a_restored_session_still_reaches_the_gateway(settings):
    """The regression this guards is silent and expensive.

    Upstream's from_dict called CollaborativeStormLMConfigs.init(), which hardcodes api_base=None
    against gpt-4o-2024-05-13, and passed no retriever so BingSearch was used. A session restored
    that way would bill an OpenAI account directly and fail on a paid search key, with nothing in
    the response to say so.
    """

    original = build_costorm_runner(settings, "a narrow topic")
    restored = load_costorm_runner(settings, original.to_dict())

    for role in COSTORM_ROLES:
        model = getattr(restored.lm_config, role)
        assert model.kwargs["api_base"] == "https://gateway.example/v1", role
        assert model.model in {"openai/ollama/fast:cloud", "openai/ollama/strong:cloud"}, role

    assert type(restored.rm).__name__ == "SearXNG"
    assert restored.encoder.embedding_model_name == "openai/ollama/embed:cloud"
    # The knowledge base captures the model by value at construction, so restoring it with the
    # wrong config would leave this one pointing at api.openai.com even if lm_config were fixed.
    assert restored.knowledge_base.gen_summary_module.engine is restored.lm_config.knowledge_base_lm


def test_a_restored_session_keeps_its_topic_and_history(settings):
    original = build_costorm_runner(settings, "a narrow topic")
    state = original.to_dict()
    restored = load_costorm_runner(settings, state)

    assert restored.runner_argument.topic == "a narrow topic"
    assert len(restored.conversation_history) == len(original.conversation_history)
    assert restored.to_dict()["runner_argument"]["topic"] == "a narrow topic"


def test_a_session_file_carries_no_credential(settings):
    """Sessions persist under the data directory for their TTL, so a key there outlives the run."""
    state = build_costorm_runner(settings, "a narrow topic").to_dict()

    assert MINIMAL["KVASIR_PROVIDER_GATEWAY_API_KEY"] not in json.dumps(state)
    for role in COSTORM_ROLES:
        assert not [key for key in state["lm_config"][role] if key.startswith("api_")]


def test_reasoning_effort_is_not_sent_unless_set(storm):
    for role in STORM_ROLES:
        assert "extra_body" not in getattr(storm.lm_configs, role).kwargs, role


def test_reasoning_effort_reaches_every_role_in_the_body(tmp_path):
    settings = Settings.from_env(MINIMAL | {"KVASIR_MODEL_REASONING_EFFORT": "none"})
    storm = build_storm_runner(settings, tmp_path)
    costorm = build_costorm_runner(settings, "a narrow topic")

    for model in _models(storm.lm_configs, STORM_ROLES) + _models(costorm.lm_config, COSTORM_ROLES):
        assert model.kwargs["extra_body"] == {"reasoning_effort": "none"}
