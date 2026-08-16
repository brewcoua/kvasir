"""No network. Building a runner makes no request; only running one does."""

import pytest

from kvasir.config import Settings
from kvasir.runners import build_costorm_runner, build_storm_runner, load_costorm_runner

MINIMAL = {
    "OPENAI_API_KEY": "key",
    "OPENAI_API_BASE": "https://gateway.example/v1",
    "KVASIR_MODEL_FAST": "openai/ollama/fast:cloud",
    "KVASIR_MODEL_STRONG": "openai/ollama/strong:cloud",
    "KVASIR_SEARXNG_URL": "http://searxng.example",
    "KVASIR_EMBEDDING_MODEL": "openai/ollama/embed:cloud",
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
    # CoStormRunner falls back to BingSearch when rm is omitted, which needs a paid key.
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
        model_strong="openai/ollama/other:cloud",
    )

    assert runner.args.search_top_k == 7
    assert runner.args.max_conv_turn == 1
    assert runner.args.max_perspective == 2
    assert runner.retriever.rm.k == 7
    assert runner.lm_configs.article_gen_lm.model == "openai/ollama/other:cloud"
    assert runner.lm_configs.conv_simulator_lm.model == "openai/ollama/fast:cloud"


def test_overrides_do_not_mutate_the_shared_settings(settings, tmp_path):
    build_storm_runner(settings, tmp_path, model_fast="openai/ollama/other:cloud")

    assert settings.model_fast == "openai/ollama/fast:cloud"


def test_a_restored_session_still_reaches_the_gateway(settings):
    """The regression this guards is silent and expensive.

    CoStormRunner.from_dict calls CollaborativeStormLMConfigs.init(), which hardcodes
    api_base=None against gpt-4o-2024-05-13, and passes no retriever so BingSearch is used. A
    session restored that way would bill an OpenAI account directly and fail on a paid search key,
    with nothing in the response to say so.
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
