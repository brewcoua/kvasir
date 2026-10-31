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
    assert settings.run_store == "memory"
    assert settings.runs_db.name == "kvasir.db"
    assert settings.max_runs == 100
    assert settings.run_ttl_days == 30
    assert settings.research_max_searches == 16
    assert settings.research_results_per_query == 3
    assert settings.research_max_attempts == 4
    assert settings.research_follow_ups is True
    assert settings.research_max_depth == 2
    assert settings.research_max_entities == 8
    assert settings.research_max_criteria == 5


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


def test_run_store_is_checked():
    assert Settings.from_env(MINIMAL | {"KVASIR_RUN_STORE": "SQLite"}).run_store == "sqlite"

    with pytest.raises(ConfigError, match="KVASIR_RUN_STORE"):
        Settings.from_env(MINIMAL | {"KVASIR_RUN_STORE": "postgres"})


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


def _write(tmp_path, text):
    path = tmp_path / "kvasir.toml"
    path.write_text(text)
    return str(path)


def test_the_config_file_stands_in_for_the_environment(tmp_path):
    path = _write(
        tmp_path,
        """
openai_api_key = "file-key"
openai_api_base = "http://localhost:11434/v1"
model_fast = "openai/qwen3.8:27b"
model_strong = "openai/qwen3.8:27b"
searxng_url = "http://localhost:8888"
max_threads = 2
log_format = "text"
reasoning_effort = "none"
""",
    )

    settings = Settings.from_env({"KVASIR_CONFIG": path})

    assert settings.openai_api_key == "file-key"
    assert settings.model_strong == "openai/qwen3.8:27b"
    assert settings.max_threads == 2
    assert settings.log_format == "text"
    assert settings.reasoning_effort == "none"


def test_a_set_variable_wins_over_the_file_and_an_empty_one_does_not(tmp_path):
    path = _write(tmp_path, 'model_fast = "openai/from-file"\nsearxng_url = "http://file"\n')

    env = MINIMAL | {"KVASIR_CONFIG": path, "KVASIR_SEARXNG_URL": ""}
    settings = Settings.from_env(env)

    assert settings.model_fast == MINIMAL["KVASIR_MODEL_FAST"]
    assert settings.searxng_url == "http://file"


@pytest.mark.parametrize(("value", "expected"), [("false", False), ("NO", False), ("1", True)])
def test_a_boolean_setting_reads_its_usual_spellings(value, expected):
    env = MINIMAL | {"KVASIR_RESEARCH_FOLLOW_UPS": value}

    assert Settings.from_env(env).research_follow_ups is expected


def test_a_boolean_setting_rejects_anything_else():
    with pytest.raises(ConfigError, match="KVASIR_RESEARCH_FOLLOW_UPS must be true or false"):
        Settings.from_env(MINIMAL | {"KVASIR_RESEARCH_FOLLOW_UPS": "maybe"})


def test_a_boolean_in_the_config_file_is_read(tmp_path):
    path = _write(tmp_path, "research_follow_ups = false\nresearch_max_searches = 8\n")

    settings = Settings.from_env(MINIMAL | {"KVASIR_CONFIG": path})

    assert settings.research_follow_ups is False
    assert settings.research_max_searches == 8


def test_reasoning_effort_defaults_to_unset():
    assert Settings.from_env(MINIMAL).reasoning_effort is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('model_fats = "openai/x"\n', "unknown keys in config file .*: model_fats"),
        ("max_threads = [2]\n", "max_threads .* must be a string, an integer or a boolean"),
        ("max_threads = true\n", "KVASIR_MAX_THREADS must be an integer, got 'true'"),
        ("model_fast = \n", "not valid TOML"),
    ],
)
def test_an_unusable_config_file_is_rejected(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(MINIMAL | {"KVASIR_CONFIG": _write(tmp_path, text)})


def test_a_missing_config_file_is_an_error_rather_than_ignored(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config file"):
        Settings.from_env(MINIMAL | {"KVASIR_CONFIG": str(tmp_path / "absent.toml")})


def test_kvasir_toml_is_found_in_the_working_directory(tmp_path, monkeypatch, default_config_file):
    _write(tmp_path, 'searxng_url = "http://from-cwd"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KVASIR_SEARXNG_URL", raising=False)
    for name, value in MINIMAL.items():
        if name != "KVASIR_SEARXNG_URL":
            monkeypatch.setenv(name, value)

    assert Settings.from_env().searxng_url == "http://from-cwd"
