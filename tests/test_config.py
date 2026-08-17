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


def test_a_model_name_is_kept_whole():
    """Only the first segment routes; everything after it is the gateway's to interpret."""
    name = "openai/ollama/deepseek-v4-flash:cloud"
    settings = Settings.from_env(MINIMAL | {"KVASIR_MODEL_FAST": name})

    assert settings.model_fast == name


@pytest.mark.parametrize("name", ["KVASIR_MODEL_FAST", "KVASIR_MODEL_STRONG"])
def test_a_model_name_without_a_provider_is_rejected(name):
    """Rejected rather than silently prefixed: a name rewritten out of sight is what confuses."""
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env(MINIMAL | {name: "gpt-4o-mini"})

    assert "openai/gpt-4o-mini" in str(excinfo.value)


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


def test_apply_environment_disables_telemetry(monkeypatch):
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY", raising=False)

    apply_environment()

    assert os.environ["DO_NOT_TRACK"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_apply_environment_exports_no_credential(monkeypatch):
    """The OPENAI_* exports existed only because litellm read them behind our back."""
    for name in ("OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    apply_environment()

    assert "OPENAI_API_KEY" not in os.environ
    assert "OPENAI_API_BASE" not in os.environ
    assert "OPENAI_BASE_URL" not in os.environ


def test_settings_are_frozen():
    settings = Settings.from_env(MINIMAL)

    with pytest.raises(AttributeError):
        settings.model_fast = "other"  # type: ignore[misc]
