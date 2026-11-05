import logging
import os
import sqlite3
from pathlib import Path

import pytest

from kvasir.config import NO_API_KEY, ConfigError, Settings, SetupRequired, apply_environment
from kvasir.settings_store import SettingsStore

ENV = {
    "KVASIR_PROVIDER_LOCAL_API_BASE": "https://gateway.example/v1",
    "KVASIR_PROVIDER_LOCAL_API_KEY": "key",
    "KVASIR_TOKEN": "token",
}
STORED = {
    "model.fast": "local/fast:cloud",
    "model.strong": "local/strong:cloud",
    "model.embedding": "local/embed:latest",
    "search.url": "http://searxng.example",
}
REQUIRED = list(STORED)


def strict(**stored):
    """Settings over STORED, raising on any bad value rather than logging it."""
    return Settings.load(
        ENV, STORED | {k.replace("__", "."): v for k, v in stored.items()}, strict=True
    )


def test_defaults():
    settings = Settings.load(ENV, STORED)

    assert str(settings.data_dir) == "/data"
    assert (settings.host, settings.port) == ("0.0.0.0", 8080)
    assert settings.sessions_dir.name == "sessions"
    assert settings.sessions.ttl_hours == 168
    assert settings.runs.max_concurrent == 1
    assert (
        settings.search.top_k == settings.storm.max_conv_turn == settings.storm.max_perspective == 3
    )
    assert settings.log.level == "INFO"
    assert settings.log.format == "json"
    assert settings.runs.store == "memory"
    assert settings.db.name == "kvasir.db"
    assert settings.runs.max == 100
    assert settings.runs.ttl_days == 30
    assert settings.runs.keep_call_content is False
    assert settings.model.checker is None
    assert settings.model.reasoning_effort is None
    assert settings.model.reason is None
    assert settings.model.reason_effort == "medium"
    assert settings.research.max_searches == 16
    assert settings.research.results_per_query == 3
    assert settings.research.max_attempts == 4
    assert settings.research.follow_ups is True
    assert settings.research.max_depth == 2
    assert settings.research.max_entities == 8
    assert settings.research.max_criteria == 5
    assert settings.missing == ()


def test_the_token_is_required():
    with pytest.raises(ConfigError, match="KVASIR_TOKEN"):
        Settings.load({k: v for k, v in ENV.items() if k != "KVASIR_TOKEN"}, STORED)


def test_nothing_stored_is_a_service_waiting_for_its_setup():
    settings = Settings.load(ENV, {})

    assert settings.missing == tuple(REQUIRED)
    with pytest.raises(SetupRequired, match="model.fast, model.strong"):
        settings.ready()


@pytest.mark.parametrize("key", REQUIRED)
def test_each_required_setting_is_reported_until_it_is_set(key):
    settings = Settings.load(ENV, {k: v for k, v in STORED.items() if k != key})

    assert settings.missing == (key,)


def test_a_required_setting_stored_empty_is_still_missing():
    assert Settings.load(ENV, STORED | {"model.fast": ""}).missing == ("model.fast",)


def test_a_provider_needs_its_api_base():
    env = {k: v for k, v in ENV.items() if k != "KVASIR_PROVIDER_LOCAL_API_BASE"}

    with pytest.raises(ConfigError, match="KVASIR_PROVIDER_LOCAL_API_BASE"):
        Settings.load(env, STORED)


def test_unprefixed_variables_are_not_read():
    env = ENV | {"OPENAI_API_KEY": "other", "LOG_LEVEL": "debug", "LOG_FORMAT": "text"}
    settings = Settings.load(env, STORED)

    assert settings.providers["local"].api_key == "key"
    assert settings.log.level == "INFO"
    assert settings.log.format == "json"


def test_a_variable_for_a_stored_setting_is_named_and_ignored(caplog):
    with caplog.at_level(logging.WARNING, "kvasir"):
        settings = Settings.load(ENV | {"KVASIR_MODEL_FAST": "local/other"}, STORED)

    assert settings.model.fast == STORED["model.fast"]
    assert "KVASIR_MODEL_FAST" in caplog.text


def test_nothing_stored_is_read_from_the_data_directory(tmp_path):
    SettingsStore(tmp_path / "kvasir.db").write({"research.max_searches": 9})

    settings = Settings.load(ENV | {"KVASIR_DATA_DIR": str(tmp_path)})

    assert settings.research.max_searches == 9
    assert settings.missing == tuple(REQUIRED)


def test_a_model_resolves_against_its_provider():
    settings = Settings.load(ENV, STORED)

    model = settings.resolve("local/deepseek-v4.1-flash:cloud")

    assert model.model == "openai/deepseek-v4.1-flash:cloud"
    assert model.api_base == "https://gateway.example/v1"
    assert model.api_key == "key"


def test_only_the_first_slash_splits_a_model_reference():
    settings = Settings.load(ENV, STORED)

    assert settings.resolve("local/ollama/model:tag").model == "openai/ollama/model:tag"


def test_resolve_names_an_unknown_provider_and_the_configured_ones():
    settings = Settings.load(ENV, STORED)

    with pytest.raises(ConfigError, match="unknown provider 'cloud'; configured: local"):
        settings.resolve("cloud/model")


@pytest.mark.parametrize("ref", ["gpt-4o-mini", "local/", ""])
def test_resolve_rejects_a_reference_without_provider_and_name(ref):
    with pytest.raises(ConfigError, match="must name a provider"):
        Settings.load(ENV, STORED).resolve(ref)


@pytest.mark.parametrize("name", ["fast", "strong", "reason", "checker", "embedding"])
def test_a_model_naming_an_unknown_provider_is_refused(name):
    with pytest.raises(ConfigError, match=f"model.{name}: .*unknown provider 'typo'"):
        strict(**{f"model__{name}": "typo/model"})


def test_a_stored_model_naming_an_unknown_provider_is_dropped(caplog):
    with caplog.at_level(logging.WARNING, "kvasir"):
        settings = Settings.load(ENV, STORED | {"model.fast": "typo/model", "model.checker": "x"})

    assert settings.model.fast == ""
    assert settings.model.checker is None
    assert settings.missing == ("model.fast",)
    assert "unknown provider 'typo'" in caplog.text


def test_a_provider_may_hold_underscores():
    env = {k: v for k, v in ENV.items() if "PROVIDER" not in k} | {
        "KVASIR_PROVIDER_MY_CLOUD_API_BASE": "https://cloud.example/v1/",
    }
    stored = {"model.fast": "my_cloud/fast", "model.strong": "my_cloud/strong"}

    settings = Settings.load(env, STORED | stored | {"model.embedding": "my_cloud/embed"})

    assert list(settings.providers) == ["my_cloud"]
    assert settings.providers["my_cloud"].api_base == "https://cloud.example/v1"


def test_a_bad_provider_name_is_refused():
    with pytest.raises(ConfigError, match="does not name a provider"):
        Settings.load(ENV | {"KVASIR_PROVIDER_A.B_API_BASE": "http://x"}, STORED)


def test_a_provider_without_a_key_gets_the_placeholder():
    env = {k: v for k, v in ENV.items() if k != "KVASIR_PROVIDER_LOCAL_API_KEY"}

    assert Settings.load(env, STORED).providers["local"].api_key == NO_API_KEY


def test_no_secret_is_in_the_settings_repr():
    text = repr(Settings.load(ENV, STORED))

    assert "token='" not in text
    assert "'key'" not in text


def test_trailing_slashes_are_stripped_from_urls():
    settings = Settings.load(
        ENV | {"KVASIR_PROVIDER_LOCAL_API_BASE": "https://gateway.example/v1/"},
        STORED | {"search.url": "http://searxng.example/"},
    )

    assert settings.providers["local"].api_base == "https://gateway.example/v1"
    assert settings.search.url == "http://searxng.example"


@pytest.mark.parametrize("value", ["nope", 3.5, 0, -1, True, "3"])
def test_rejects_unusable_integers(value):
    with pytest.raises(ConfigError, match="storm.max_conv_turn"):
        strict(storm__max_conv_turn=value)


def test_the_page_age_may_be_zero():
    assert strict(fetch__max_age_hours=0).fetch.max_age_hours == 0


def test_a_bad_stored_value_falls_back_to_its_default(caplog):
    with caplog.at_level(logging.WARNING, "kvasir"):
        settings = Settings.load(ENV, STORED | {"storm.max_conv_turn": "many"})

    assert settings.storm.max_conv_turn == 3
    assert "storm.max_conv_turn must be an integer" in caplog.text


def test_an_unknown_stored_key_is_refused_or_ignored():
    with pytest.raises(ConfigError, match="unknown setting model.fats"):
        strict(model__fats="local/x")

    assert Settings.load(ENV, STORED | {"model.fats": "local/x"}).model.fast == STORED["model.fast"]


@pytest.mark.parametrize(
    ("variable", "value"), [("KVASIR_MAX_THREADS", "nope"), ("KVASIR_PORT", "0")]
)
def test_a_bad_variable_stops_the_start(variable, value):
    with pytest.raises(ConfigError, match=variable):
        Settings.load(ENV | {variable: value}, STORED)


def test_top_level_settings_read_from_the_environment():
    settings = Settings.load(
        ENV
        | {
            "KVASIR_DATA_DIR": "/srv/kvasir",
            "KVASIR_MAX_THREADS": "5",
            "KVASIR_HOST": "127.0.0.1",
            "KVASIR_PORT": "9000",
        },
        STORED,
    )

    assert settings.data_dir == Path("/srv/kvasir")
    assert settings.max_threads == 5
    assert (settings.host, settings.port) == ("127.0.0.1", 9000)


def test_log_level_is_upper_cased():
    assert Settings.load(ENV | {"KVASIR_LOG_LEVEL": "debug"}, STORED).log.level == "DEBUG"


def test_log_format_is_checked():
    assert Settings.load(ENV | {"KVASIR_LOG_FORMAT": "TEXT"}, STORED).log.format == "text"

    with pytest.raises(ConfigError, match="KVASIR_LOG_FORMAT"):
        Settings.load(ENV | {"KVASIR_LOG_FORMAT": "logfmt"}, STORED)


def test_run_store_is_checked():
    assert strict(runs__store="SQLite").runs.store == "sqlite"

    with pytest.raises(ConfigError, match="runs.store"):
        strict(runs__store="postgres")


def test_a_boolean_setting_must_be_a_boolean():
    assert strict(research__follow_ups=False).research.follow_ups is False

    for value in ("false", 0, None):
        with pytest.raises(ConfigError, match="research.follow_ups must be true or false"):
            strict(research__follow_ups=value)


def test_an_optional_string_stored_empty_is_unset():
    assert strict(model__reasoning_effort="").model.reasoning_effort is None
    assert strict(model__reasoning_effort="none").model.reasoning_effort == "none"


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
    settings = Settings.load(ENV, STORED)

    with pytest.raises(AttributeError):
        settings.model = None  # type: ignore[misc]
    with pytest.raises(AttributeError):
        settings.model.fast = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        settings.providers["other"] = None  # type: ignore[index]


def test_the_store_keeps_values_until_they_are_reset(tmp_path):
    store = SettingsStore(tmp_path / "kvasir.db")
    store.write({"research.max_searches": 9, "sources.deny": ["farm.example"]})
    store.write({"research.max_searches": None, "model.reason": "local/r"})

    assert SettingsStore(tmp_path / "kvasir.db").load() == {
        "sources.deny": '["farm.example"]',
        "model.reason": '"local/r"',
    }


def test_fetch_settings_default_to_no_contact_and_a_second():
    settings = Settings.load(ENV, STORED)

    assert settings.fetch.contact == ""
    assert settings.fetch.host_interval_seconds == 1.0
    assert (settings.fetch.render, settings.fetch.stealth) == ("auto", False)


def test_fetch_settings_are_read():
    settings = strict(
        fetch__contact="ops@example.org",
        fetch__host_interval_seconds=0.5,
        fetch__render="OFF",
        fetch__stealth=True,
    )

    assert settings.fetch.contact == "ops@example.org"
    assert settings.fetch.host_interval_seconds == 0.5
    assert (settings.fetch.render, settings.fetch.stealth) == ("off", True)
    assert strict(fetch__host_interval_seconds=2).fetch.host_interval_seconds == 2.0

    with pytest.raises(ConfigError, match="fetch.render"):
        strict(fetch__render="always")


@pytest.mark.parametrize("value", ["soon", -1, float("nan"), float("inf"), True])
def test_the_host_interval_must_be_a_finite_number_of_seconds(value):
    with pytest.raises(ConfigError, match="fetch.host_interval_seconds"):
        strict(fetch__host_interval_seconds=value)


def test_favicon_settings_default_to_the_direct_provider():
    settings = Settings.load(ENV, STORED)

    assert settings.favicons.providers == ("direct",)
    assert settings.favicons.custom_url == ""
    assert (settings.favicons.ttl_days, settings.favicons.miss_ttl_days) == (30, 3)
    assert settings.favicons_db == Path("/data/favicons.db")


def test_favicon_providers_are_read_in_order():
    settings = strict(
        favicons__providers=["Google", " direct ", "duckduckgo"],
        favicons__ttl_days=7,
        favicons__miss_ttl_days=1,
    )

    assert settings.favicons.providers == ("google", "direct", "duckduckgo")
    assert (settings.favicons.ttl_days, settings.favicons.miss_ttl_days) == (7, 1)


def test_an_empty_provider_list_turns_favicons_off():
    assert strict(favicons__providers=[]).favicons.providers == ()


@pytest.mark.parametrize(
    ("value", "message"),
    [(["direct", "bing"], "'bing'"), ("direct", "list of strings"), ([1], "list of strings")],
)
def test_a_bad_favicon_provider_list_is_rejected(value, message):
    with pytest.raises(ConfigError, match=f"favicons.providers.*{message}"):
        strict(favicons__providers=value)


@pytest.mark.parametrize("url", ["", "https://icons.example/x.png", "ftp://{domain}/x", "{domain}"])
def test_the_custom_provider_needs_a_url_template_with_a_domain(url):
    with pytest.raises(ConfigError, match="favicons.custom_url"):
        strict(favicons__providers=["custom"], favicons__custom_url=url)


def test_a_bad_stored_favicon_section_falls_back_to_its_defaults():
    stored = STORED | {"favicons.providers": ["custom"], "favicons.ttl_days": 7}

    assert Settings.load(ENV, stored).favicons.providers == ("direct",)


def test_the_custom_url_is_not_checked_when_the_provider_is_unused():
    strict(favicons__custom_url="nonsense")


def test_source_settings_default_to_off_with_the_cisa_list():
    settings = Settings.load(ENV, STORED)

    assert settings.sources.enabled is False
    assert settings.sources.wikidata is True
    assert settings.sources.rsp is False
    assert (settings.sources.ttl_days, settings.sources.miss_ttl_days) == (30, 7)
    assert settings.sources.allow == settings.sources.deny == ()
    assert [entry.partition("=")[0] for entry in settings.sources.lists] == ["government"]
    assert settings.sources_db == Path("/data/sources.db")


def test_source_lists_keep_their_case():
    settings = strict(
        sources__enabled=True,
        sources__rsp=True,
        sources__allow=["example.org", " Example.com"],
        sources__deny=["farm.example"],
        sources__lists=["ai_generated=https://lists.example/Ai.txt"],
    )

    assert settings.sources.enabled is True
    assert settings.sources.rsp is True
    assert settings.sources.allow == ("example.org", "Example.com")
    assert settings.sources.deny == ("farm.example",)
    assert settings.sources.lists == ("ai_generated=https://lists.example/Ai.txt",)


def test_an_empty_source_list_setting_turns_the_default_lists_off():
    assert strict(sources__lists=[]).sources.lists == ()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("endpoint", "nonsense"),
        ("lists", ["https://lists.example/a.txt"]),
        ("lists", ["government=ftp://lists.example/a.txt"]),
        ("lists", ["shiny=https://lists.example/a.txt"]),
        ("allow", ["10.0.0.1"]),
        ("deny", ["nonsense"]),
    ],
)
def test_a_bad_source_setting_is_rejected(key, value):
    with pytest.raises(ConfigError, match=f"sources.{key}"):
        strict(sources__enabled=True, **{f"sources__{key}": value})


def test_source_settings_are_not_checked_while_sources_are_off():
    assert strict(sources__endpoint="nonsense").sources.enabled is False


def test_a_source_host_may_end_in_a_dot():
    assert strict(sources__enabled=True, sources__deny=["farm.example."]).sources.deny == (
        "farm.example.",
    )


def test_a_row_that_is_not_json_costs_only_its_field(tmp_path, caplog):
    store = SettingsStore(tmp_path / "kvasir.db")
    store.write({"research.max_searches": 9})
    with sqlite3.connect(tmp_path / "kvasir.db") as db:
        db.execute("INSERT INTO settings VALUES ('research.max_depth', '{broken')")

    settings = Settings.load(ENV | {"KVASIR_DATA_DIR": str(tmp_path)})

    assert settings.research.max_searches == 9
    assert settings.research.max_depth == 2
    assert "research.max_depth is not JSON" in caplog.text
    with pytest.raises(ConfigError, match="research.max_depth"):
        Settings.load(ENV | {"KVASIR_DATA_DIR": str(tmp_path)}, strict=True)


def test_loading_creates_no_database(tmp_path):
    settings = Settings.load(ENV | {"KVASIR_DATA_DIR": str(tmp_path / "absent")})

    assert settings.missing == tuple(REQUIRED)
    assert not (tmp_path / "absent").exists()
