import os
from pathlib import Path

import pytest

from kvasir.config import NO_API_KEY, ConfigError, Settings, apply_environment

MINIMAL = {
    "KVASIR_PROVIDER_LOCAL_API_BASE": "https://gateway.example/v1",
    "KVASIR_PROVIDER_LOCAL_API_KEY": "key",
    "KVASIR_MODEL_FAST": "local/fast:cloud",
    "KVASIR_MODEL_STRONG": "local/strong:cloud",
    "KVASIR_MODEL_EMBEDDING": "local/embed:latest",
    "KVASIR_SEARCH_URL": "http://searxng.example",
}
REQUIRED = [name for name in MINIMAL if "PROVIDER" not in name]


def test_defaults():
    settings = Settings.from_env(MINIMAL)

    assert str(settings.data_dir) == "/data"
    assert settings.sessions_dir.name == "sessions"
    assert settings.sessions.ttl_hours == 168
    assert settings.runs.max_concurrent == 1
    assert (
        settings.search.top_k == settings.storm.max_conv_turn == settings.storm.max_perspective == 3
    )
    assert settings.log.level == "INFO"
    assert settings.log.format == "json"
    assert settings.runs.store == "memory"
    assert settings.runs_db.name == "kvasir.db"
    assert settings.runs.max == 100
    assert settings.runs.ttl_days == 30
    assert settings.runs.keep_call_content is False
    assert settings.model.checker is None
    assert settings.model.reasoning_effort is None
    assert settings.research.max_searches == 16
    assert settings.research.results_per_query == 3
    assert settings.research.max_attempts == 4
    assert settings.research.follow_ups is True
    assert settings.research.max_depth == 2
    assert settings.research.max_entities == 8
    assert settings.research.max_criteria == 5


@pytest.mark.parametrize("name", REQUIRED)
def test_each_required_variable_is_required(name):
    env = MINIMAL | {name: ""}

    with pytest.raises(ConfigError, match=name):
        Settings.from_env(env)


def test_all_missing_variables_are_reported_together():
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({})

    for name in REQUIRED:
        assert name in str(excinfo.value)


def test_a_provider_needs_its_api_base():
    env = {k: v for k, v in MINIMAL.items() if k != "KVASIR_PROVIDER_LOCAL_API_BASE"}

    with pytest.raises(ConfigError, match="KVASIR_PROVIDER_LOCAL_API_BASE"):
        Settings.from_env(env)


def test_unprefixed_variables_are_no_longer_read():
    env = MINIMAL | {"OPENAI_API_KEY": "other", "LOG_LEVEL": "debug", "LOG_FORMAT": "text"}
    settings = Settings.from_env(env)

    assert settings.providers["local"].api_key == "key"
    assert settings.log.level == "INFO"
    assert settings.log.format == "json"


def test_a_model_resolves_against_its_provider():
    settings = Settings.from_env(MINIMAL)

    model = settings.resolve("local/deepseek-v4.1-flash:cloud")

    assert model.model == "openai/deepseek-v4.1-flash:cloud"
    assert model.api_base == "https://gateway.example/v1"
    assert model.api_key == "key"


def test_only_the_first_slash_splits_a_model_reference():
    settings = Settings.from_env(MINIMAL)

    assert settings.resolve("local/ollama/model:tag").model == "openai/ollama/model:tag"


def test_resolve_names_an_unknown_provider_and_the_configured_ones():
    settings = Settings.from_env(MINIMAL)

    with pytest.raises(ConfigError, match="unknown provider 'cloud'; configured: local"):
        settings.resolve("cloud/model")


@pytest.mark.parametrize("ref", ["gpt-4o-mini", "local/", ""])
def test_resolve_rejects_a_reference_without_provider_and_name(ref):
    with pytest.raises(ConfigError, match="must name a provider"):
        Settings.from_env(MINIMAL).resolve(ref)


@pytest.mark.parametrize(
    "name",
    ["KVASIR_MODEL_FAST", "KVASIR_MODEL_STRONG", "KVASIR_MODEL_CHECKER", "KVASIR_MODEL_EMBEDDING"],
)
def test_a_model_naming_an_unknown_provider_fails_at_startup(name):
    with pytest.raises(ConfigError, match=f"{name}: .*unknown provider 'typo'"):
        Settings.from_env(MINIMAL | {name: "typo/model"})


def test_a_model_without_a_provider_fails_at_startup():
    with pytest.raises(ConfigError, match="KVASIR_MODEL_FAST: .*must name a provider"):
        Settings.from_env(MINIMAL | {"KVASIR_MODEL_FAST": "gpt-4o-mini"})


def test_a_provider_may_come_from_the_environment_alone_and_its_name_may_hold_underscores():
    env = {k: v for k, v in MINIMAL.items() if "PROVIDER" not in k} | {
        "KVASIR_PROVIDER_MY_CLOUD_API_BASE": "https://cloud.example/v1/",
        "KVASIR_MODEL_FAST": "my_cloud/fast",
        "KVASIR_MODEL_STRONG": "my_cloud/strong",
        "KVASIR_MODEL_EMBEDDING": "my_cloud/embed",
    }

    settings = Settings.from_env(env)

    assert list(settings.providers) == ["my_cloud"]
    assert settings.providers["my_cloud"].api_base == "https://cloud.example/v1"


def test_a_provider_without_a_key_gets_the_placeholder():
    env = {k: v for k, v in MINIMAL.items() if k != "KVASIR_PROVIDER_LOCAL_API_KEY"}

    assert Settings.from_env(env).providers["local"].api_key == NO_API_KEY


def test_trailing_slashes_are_stripped_from_urls():
    settings = Settings.from_env(
        MINIMAL
        | {
            "KVASIR_PROVIDER_LOCAL_API_BASE": "https://gateway.example/v1/",
            "KVASIR_SEARCH_URL": "http://searxng.example/",
        }
    )

    assert settings.providers["local"].api_base == "https://gateway.example/v1"
    assert settings.search.url == "http://searxng.example"


@pytest.mark.parametrize("value", ["nope", "3.5", "0", "-1", ""])
def test_rejects_unusable_integers(value):
    env = MINIMAL | {"KVASIR_STORM_MAX_CONV_TURN": value}

    if value == "":
        assert Settings.from_env(env).storm.max_conv_turn == 3
    else:
        with pytest.raises(ConfigError, match="KVASIR_STORM_MAX_CONV_TURN"):
            Settings.from_env(env)


def test_top_level_settings_read_from_the_environment():
    settings = Settings.from_env(
        MINIMAL | {"KVASIR_DATA_DIR": "/srv/kvasir", "KVASIR_MAX_THREADS": "5"}
    )

    assert settings.data_dir == Path("/srv/kvasir")
    assert settings.max_threads == 5


def test_log_level_is_upper_cased():
    assert Settings.from_env(MINIMAL | {"KVASIR_LOG_LEVEL": "debug"}).log.level == "DEBUG"


def test_log_format_is_checked():
    assert Settings.from_env(MINIMAL | {"KVASIR_LOG_FORMAT": "TEXT"}).log.format == "text"

    with pytest.raises(ConfigError, match="KVASIR_LOG_FORMAT"):
        Settings.from_env(MINIMAL | {"KVASIR_LOG_FORMAT": "logfmt"})


def test_run_store_is_checked():
    assert Settings.from_env(MINIMAL | {"KVASIR_RUNS_STORE": "SQLite"}).runs.store == "sqlite"

    with pytest.raises(ConfigError, match="KVASIR_RUNS_STORE"):
        Settings.from_env(MINIMAL | {"KVASIR_RUNS_STORE": "postgres"})


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
        settings.model = None  # type: ignore[misc]
    with pytest.raises(AttributeError):
        settings.model.fast = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        settings.providers["other"] = None  # type: ignore[index]


def _write(tmp_path, text):
    path = tmp_path / "kvasir.toml"
    path.write_text(text)
    return str(path)


def test_the_config_file_stands_in_for_the_environment(tmp_path):
    path = _write(
        tmp_path,
        """
max_threads = 2

[provider.local]
api_base = "http://localhost:11434/v1"
api_key = "file-key"

[model]
fast = "local/qwen3.8:27b"
strong = "local/qwen3.8:27b"
embedding = "local/embed"
reasoning_effort = "none"

[search]
url = "http://localhost:8888"

[log]
format = "text"
""",
    )

    settings = Settings.from_env({"KVASIR_CONFIG": path})

    assert settings.providers["local"].api_key == "file-key"
    assert settings.model.strong == "local/qwen3.8:27b"
    assert settings.max_threads == 2
    assert settings.log.format == "text"
    assert settings.model.reasoning_effort == "none"


def test_a_set_variable_wins_over_the_file_and_an_empty_one_does_not(tmp_path):
    path = _write(tmp_path, '[model]\nfast = "local/from-file"\n[search]\nurl = "http://file"\n')

    env = MINIMAL | {"KVASIR_CONFIG": path, "KVASIR_SEARCH_URL": ""}
    settings = Settings.from_env(env)

    assert settings.model.fast == MINIMAL["KVASIR_MODEL_FAST"]
    assert settings.search.url == "http://file"


def test_an_environment_provider_merges_over_the_file_one_field_by_field(tmp_path):
    path = _write(
        tmp_path,
        '[provider.local]\napi_base = "http://file/v1"\napi_key = "file-key"\n',
    )
    env = {k: v for k, v in MINIMAL.items() if "PROVIDER" not in k} | {
        "KVASIR_CONFIG": path,
        "KVASIR_PROVIDER_LOCAL_API_KEY": "env-key",
    }

    provider = Settings.from_env(env).providers["local"]

    assert provider.api_key == "env-key"
    assert provider.api_base == "http://file/v1"


@pytest.mark.parametrize(("value", "expected"), [("false", False), ("NO", False), ("1", True)])
def test_a_boolean_setting_reads_its_usual_spellings(value, expected):
    env = MINIMAL | {"KVASIR_RESEARCH_FOLLOW_UPS": value}

    assert Settings.from_env(env).research.follow_ups is expected


def test_a_boolean_setting_rejects_anything_else():
    with pytest.raises(ConfigError, match="KVASIR_RESEARCH_FOLLOW_UPS must be true or false"):
        Settings.from_env(MINIMAL | {"KVASIR_RESEARCH_FOLLOW_UPS": "maybe"})


def test_a_boolean_in_the_config_file_is_read(tmp_path):
    path = _write(tmp_path, "[research]\nfollow_ups = false\nmax_searches = 8\n")

    settings = Settings.from_env(MINIMAL | {"KVASIR_CONFIG": path})

    assert settings.research.follow_ups is False
    assert settings.research.max_searches == 8


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[model]\nfats = "local/x"\n', "unknown keys in config file .*: model.fats"),
        ('[modle]\nfast = "local/x"\n', "unknown keys in config file .*: modle"),
        ('model_fast = "local/x"\n', "unknown keys in config file .*: model_fast"),
        ('[provider.local]\napi_url = "x"\n', "unknown keys .*: provider.local.api_url"),
        ('[provider.Local]\napi_base = "x"\n', "provider 'Local' .*lowercase letters"),
        ('provider = "local"\n', "provider in config file .* must be a table"),
        ('[provider]\nlocal = "x"\n', "provider.local in config file .* must be a table"),
        ('model = "local/x"\n', "model in config file .* must be a table"),
        ("max_threads = [2]\n", "max_threads .* must be a string, an integer or a boolean"),
        ("[runs]\nmax = [2]\n", "runs.max .* must be a string, an integer or a boolean"),
        ("max_threads = true\n", "KVASIR_MAX_THREADS must be an integer, got 'true'"),
        ("[model]\nfast = \n", "not valid TOML"),
    ],
)
def test_an_unusable_config_file_is_rejected(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(MINIMAL | {"KVASIR_CONFIG": _write(tmp_path, text)})


def test_a_missing_config_file_is_an_error_rather_than_ignored(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config file"):
        Settings.from_env(MINIMAL | {"KVASIR_CONFIG": str(tmp_path / "absent.toml")})


def test_kvasir_toml_is_found_in_the_working_directory(tmp_path, monkeypatch, default_config_file):
    _write(tmp_path, '[search]\nurl = "http://from-cwd"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KVASIR_SEARCH_URL", raising=False)
    for name, value in MINIMAL.items():
        if name != "KVASIR_SEARCH_URL":
            monkeypatch.setenv(name, value)

    assert Settings.from_env().search.url == "http://from-cwd"


def test_the_example_file_loads():
    example = Path(__file__).parent.parent / "kvasir.example.toml"

    settings = Settings.from_env({"KVASIR_CONFIG": str(example)})

    assert settings.resolve(settings.model.strong).api_key == NO_API_KEY
    assert settings.runs.store == "sqlite"
