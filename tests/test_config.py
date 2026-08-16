import os

import pytest

from kvasir.config import DEFAULT_EMBEDDING_MODEL, ConfigError, Settings, apply_environment

MINIMAL = {
    "OPENAI_API_KEY": "key",
    "OPENAI_API_BASE": "https://gateway.example/v1",
    "KVASIR_MODEL_FAST": "openai/ollama/fast:cloud",
    "KVASIR_MODEL_STRONG": "openai/ollama/strong:cloud",
    "KVASIR_SEARXNG_URL": "http://searxng.example",
}


def test_defaults():
    settings = Settings.from_env(MINIMAL)

    assert settings.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert str(settings.data_dir) == "/data"
    assert settings.sessions_dir.name == "sessions"
    assert settings.session_ttl_hours == 168
    assert settings.max_concurrent_runs == 1
    assert settings.search_top_k == settings.max_conv_turn == settings.max_perspective == 3
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"


@pytest.mark.parametrize("name", sorted(MINIMAL))
def test_each_required_variable_is_required(name):
    env = MINIMAL | {name: ""}

    with pytest.raises(ConfigError, match=name):
        Settings.from_env(env)


def test_all_missing_variables_are_reported_together():
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({})

    for name in MINIMAL:
        assert name in str(excinfo.value)


def test_model_names_reach_the_gateway_verbatim():
    name = "openai/ollama/deepseek-v4-flash:cloud"
    settings = Settings.from_env(MINIMAL | {"KVASIR_MODEL_FAST": name})

    assert settings.model_fast == name


def test_trailing_slashes_are_stripped_from_urls():
    settings = Settings.from_env(
        MINIMAL
        | {
            "OPENAI_API_BASE": "https://gateway.example/v1/",
            "KVASIR_SEARXNG_URL": "http://searxng.example/",
        }
    )

    assert settings.openai_api_base == "https://gateway.example/v1"
    assert settings.searxng_url == "http://searxng.example"


@pytest.mark.parametrize("value", ["nope", "3.5", "0", "-1", ""])
def test_rejects_unusable_integers(value):
    env = MINIMAL | {"KVASIR_MAX_CONV_TURN": value}

    if value == "":
        assert Settings.from_env(env).max_conv_turn == 3
    else:
        with pytest.raises(ConfigError, match="KVASIR_MAX_CONV_TURN"):
            Settings.from_env(env)


def test_log_level_is_upper_cased():
    assert Settings.from_env(MINIMAL | {"LOG_LEVEL": "debug"}).log_level == "DEBUG"


def test_log_format_is_checked():
    assert Settings.from_env(MINIMAL | {"LOG_FORMAT": "TEXT"}).log_format == "text"

    with pytest.raises(ConfigError, match="LOG_FORMAT"):
        Settings.from_env(MINIMAL | {"LOG_FORMAT": "logfmt"})


def test_apply_environment_exports_what_litellm_reads(monkeypatch):
    for name in ("OPENAI_API_BASE", "OPENAI_BASE_URL", "DO_NOT_TRACK"):
        monkeypatch.delenv(name, raising=False)

    apply_environment(Settings.from_env(MINIMAL))

    # Both spellings, because litellm reads one or the other depending on the code path taken.
    # Models and embeddings are given the base explicitly, so these are a backstop against a
    # default pointing at api.openai.com rather than the route to the gateway.
    assert os.environ["OPENAI_API_BASE"] == "https://gateway.example/v1"
    assert os.environ["OPENAI_BASE_URL"] == "https://gateway.example/v1"
    assert os.environ["DO_NOT_TRACK"] == "1"


def test_telemetry_is_off():
    from kvasir.storm.runtime import litellm

    assert litellm.telemetry is False


def test_the_cache_lives_under_the_data_directory():
    settings = Settings.from_env(MINIMAL | {"KVASIR_DATA_DIR": "/srv/kvasir"})

    assert str(settings.cache_dir) == "/srv/kvasir/cache"


def test_settings_are_frozen():
    settings = Settings.from_env(MINIMAL)

    with pytest.raises(AttributeError):
        settings.model_fast = "other"  # type: ignore[misc]
