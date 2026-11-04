"""Configuration parsing. The only place in the service that reads os.environ or the config file.

Configuration is environment variables, optionally backed by a TOML file. The file mirrors the
`Settings` dataclasses: top-level scalars, one table per section, and one `[provider.<name>]` table
per model provider. A variable that is set wins over the file. Missing required values raise at
startup rather than on the first request.

Every setting has one variable name derived from its path: `KVASIR_<SECTION>_<KEY>`, or
`KVASIR_<KEY>` for a top-level scalar, so `[research] max_searches` is KVASIR_RESEARCH_MAX_SEARCHES.
A provider is `KVASIR_PROVIDER_<NAME>_API_BASE` and `KVASIR_PROVIDER_<NAME>_API_KEY`, which merge
over the file's provider of the same name field by field, so a key can stay out of the file.

Every provider is an OpenAI-compatible endpoint. A model is written `<provider>/<name>` and
`Settings.resolve` turns it into what dspy needs, with the endpoint and key given explicitly;
nothing exports them into the environment.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import MISSING, Field, dataclass, field, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any

from kvasir.storm.runtime import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_HOST_INTERVAL_SECONDS,
    DEFAULT_MAX_THREADS,
)

# Read when KVASIR_CONFIG is unset, if it exists, so a local start needs no variable at all.
DEFAULT_CONFIG_FILE = Path("kvasir.toml")

# The OpenAI client refuses an empty key, and Ollama ignores whatever it is sent.
NO_API_KEY = "none"

_PROVIDER_NAME = re.compile(r"[a-z0-9_-]+")
_PROVIDER_PREFIX = "KVASIR_PROVIDER_"
_PROVIDER_RULE = "names are lowercase letters, digits, _ and -"


class ConfigError(Exception):
    """Configuration is missing or unusable. Raised before the server starts serving."""


def _choice(*options: str) -> dict[str, Any]:
    return {"norm": str.lower, "choices": options}


@dataclass(frozen=True, slots=True)
class Provider:
    """An OpenAI-compatible endpoint models are served from."""

    api_base: str
    api_key: str = NO_API_KEY


@dataclass(frozen=True, slots=True)
class Model:
    """A model reference resolved against its provider, as the clients are given it."""

    # Prefixed `openai/` because dspy routes on the leading segment and consumes it; litellm then
    # reaches the endpoint through its OpenAI client, which is what every provider speaks.
    model: str
    api_base: str
    api_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelSettings:
    """Models are `<provider>/<name>`, the provider being a `[provider.<name>]` table."""

    fast: str
    strong: str
    # The model for judgements, which are worth thinking about. Unset falls back to `strong`.
    reason: str | None = None
    # No default fits every provider, and an embedding model cannot be swapped for another's.
    embedding: str
    # A grounding checker such as MiniCheck, or none. See `kvasir.research.ground`.
    checker: str | None = None
    # Sent only when set. Unset leaves the gateway's default, which for a thinking model
    # served by Ollama spends each role's token budget on reasoning before any answer.
    reasoning_effort: str | None = None
    # Sent to the reason tier instead of `reasoning_effort`: that tier is meant to think, and its
    # token budgets leave room for it.
    reason_effort: str | None = "medium"
    # The context window the models are served with, such as Ollama's `num_ctx`. It sizes
    # how much of a page one intake call reads, the room left for its answer, and a section's.
    context_tokens: int = DEFAULT_CONTEXT_TOKENS


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchSettings:
    url: str = field(metadata={"norm": lambda value: value.rstrip("/")})
    top_k: int = 3


@dataclass(frozen=True, slots=True, kw_only=True)
class RunSettings:
    # Memory by default: a client such as Open WebUI keeps its own record of every answer,
    # so history across restarts is for debugging the service itself.
    store: str = field(default="memory", metadata=_choice("memory", "sqlite"))
    max: int = 100
    ttl_days: int = 30
    # Off by default: prompts carry the pages read and the user's question, which some
    # deployments must not retain. Timing and tokens are kept either way.
    keep_call_content: bool = False
    max_concurrent: int = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionSettings:
    ttl_hours: int = 168


@dataclass(frozen=True, slots=True, kw_only=True)
class ResearchSettings:
    """The deep research pipeline's defaults, which a request overrides one by one. Searches
    are the budget: each costs about nine page reads."""

    max_searches: int = 16
    results_per_query: int = 3
    max_attempts: int = 4
    follow_ups: bool = True
    max_depth: int = 2
    max_entities: int = 8
    max_criteria: int = 5


@dataclass(frozen=True, slots=True, kw_only=True)
class StormSettings:
    max_conv_turn: int = 3
    max_perspective: int = 3


@dataclass(frozen=True, slots=True, kw_only=True)
class LogSettings:
    level: str = field(default="INFO", metadata={"norm": str.upper})
    # JSON by default: this runs as a container whose stdout goes to a log shipper, and
    # a run's lines interleave across threads, so they need to be parseable rather than
    # readable. `text` is for reading them by eye during development.
    format: str = field(default="json", metadata=_choice("json", "text"))


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchSettings:
    # Named in the user agent so a site can reach whoever runs this. Empty names no one.
    contact: str = ""
    # Between the starts of two requests to one host. 0 turns the wait off.
    host_interval_seconds: float = DEFAULT_HOST_INTERVAL_SECONDS
    # Whether a page that needs JavaScript is rendered in a browser. Needs the browser extra.
    render: str = field(default="auto", metadata=_choice("auto", "off"))
    # Renders through Patchright, which hides the signs of automation. Getting past a challenge can
    # break a site's terms, so it is the operator's call and off by default.
    stealth: bool = False
    # How long a cached page is served without asking its site, then it is revalidated. 0 fetches
    # every page again.
    max_age_hours: int = 48


# Keyed by the Settings field and the table name in the file.
_SECTIONS: dict[str, Any] = {
    "model": ModelSettings,
    "search": SearchSettings,
    "runs": RunSettings,
    "sessions": SessionSettings,
    "research": ResearchSettings,
    "storm": StormSettings,
    "fetch": FetchSettings,
    "log": LogSettings,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """Resolved configuration. Build with `Settings.from_env()`."""

    data_dir: Path = Path("/data")
    # How wide each of the pipeline's thread pools runs, and how many search or page
    # requests may be in flight at once across the whole process.
    max_threads: int = DEFAULT_MAX_THREADS
    providers: Mapping[str, Provider]
    model: ModelSettings
    search: SearchSettings
    runs: RunSettings
    sessions: SessionSettings
    research: ResearchSettings
    storm: StormSettings
    fetch: FetchSettings
    log: LogSettings

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def runs_db(self) -> Path:
        return self.data_dir / "kvasir.db"

    @property
    def pages_db(self) -> Path:
        return self.data_dir / "pages.db"

    def resolve(self, ref: str) -> Model:
        """A `<provider>/<name>` reference as the model to call. Only the first `/` splits, since
        the name may hold more: `local/deepseek-v4.1-flash:cloud`, `cloud/org/model:tag`."""
        provider, slash, name = ref.partition("/")
        if not slash or not name:
            raise ConfigError(f"model {ref!r} must name a provider, as in <provider>/<name>")
        if provider not in self.providers:
            configured = ", ".join(sorted(self.providers)) or "none"
            raise ConfigError(
                f"model {ref!r} names unknown provider {provider!r}; configured: {configured}"
            )
        endpoint = self.providers[provider]
        return Model(model=f"openai/{name}", api_base=endpoint.api_base, api_key=endpoint.api_key)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Resolve from `env`, defaulting to os.environ, over the config file.

        The file is KVASIR_CONFIG if set. `kvasir.toml` in the working directory is only picked up
        for os.environ, so a mapping passed in is never mixed with whatever file sits nearby.
        """
        if env is None:
            env = os.environ
            path = env.get("KVASIR_CONFIG", "").strip() or (
                DEFAULT_CONFIG_FILE if DEFAULT_CONFIG_FILE.is_file() else None
            )
        else:
            path = env.get("KVASIR_CONFIG", "").strip() or None
        env = {k: v.strip() for k, v in env.items() if k.startswith("KVASIR_") and v.strip()}
        if path is not None:
            env = _read_file(Path(path)) | env

        raw_providers = _raw_providers(env)
        missing = [
            f"{_PROVIDER_PREFIX}{name.upper()}_API_BASE"
            for name, values in raw_providers.items()
            if "api_base" not in values
        ]
        slots = [(None, f) for f in _TOP_LEVEL] + [
            (section, f) for section, section_cls in _SECTIONS.items() for f in fields(section_cls)
        ]
        missing += [
            _env_name(section, f.name)
            for section, f in slots
            if f.default is MISSING and _env_name(section, f.name) not in env
        ]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")

        providers = {
            name: Provider(
                api_base=values["api_base"].rstrip("/"),
                api_key=values.get("api_key", NO_API_KEY),
            )
            for name, values in raw_providers.items()
        }
        settings = cls(
            providers=MappingProxyType(providers),
            **{f.name: _read(f, _env_name(None, f.name), env) for f in _TOP_LEVEL},
            **{
                section: section_cls(
                    **{
                        f.name: _read(f, _env_name(section, f.name), env)
                        for f in fields(section_cls)
                    }
                )
                for section, section_cls in _SECTIONS.items()
            },
        )

        # A typo in a provider name should stop the start, not the first request that uses it.
        refs = {
            "KVASIR_MODEL_FAST": settings.model.fast,
            "KVASIR_MODEL_STRONG": settings.model.strong,
            "KVASIR_MODEL_REASON": settings.model.reason,
            "KVASIR_MODEL_CHECKER": settings.model.checker,
            "KVASIR_MODEL_EMBEDDING": settings.model.embedding,
        }
        for name, ref in refs.items():
            if ref is not None:
                try:
                    settings.resolve(ref)
                except ConfigError as exc:
                    raise ConfigError(f"{name}: {exc}") from None
        return settings


# The scalars that sit directly in the file, outside any table.
_TOP_LEVEL = tuple(f for f in fields(Settings) if f.name != "providers" and f.name not in _SECTIONS)


def _env_name(section: str | None, key: str) -> str:
    return (f"KVASIR_{section}_{key}" if section else f"KVASIR_{key}").upper()


def _raw_providers(env: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """The providers `env` names, each as its set fields. Parsed by fixed suffix, so a provider
    name may itself contain `_`."""
    keys = [f.name for f in fields(Provider)]
    found: dict[str, dict[str, str]] = {}
    for name, value in env.items():
        for key in keys:
            suffix = f"_{key.upper()}"
            if name.startswith(_PROVIDER_PREFIX) and name.endswith(suffix):
                provider = name[len(_PROVIDER_PREFIX) : -len(suffix)].lower()
                if not _PROVIDER_NAME.fullmatch(provider):
                    raise ConfigError(f"{name} does not name a provider: {_PROVIDER_RULE}")
                found.setdefault(provider, {})[key] = value
    return found


def _read_file(path: Path) -> dict[str, str]:
    """The config file as the environment variables it stands for."""
    try:
        data = tomllib.loads(path.read_text())
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid TOML: {exc}") from None

    def table(value: object, key: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ConfigError(f"{key} in config file {path} must be a table")
        return value

    def text(value: object, key: str) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str | int | float):
            return str(value)
        raise ConfigError(f"{key} in config file {path} must be a string, a number or a boolean")

    provider_keys = {f.name for f in fields(Provider)}
    top_level = {f.name for f in _TOP_LEVEL}
    values: dict[str, str] = {}
    unknown: list[str] = []
    for key, value in data.items():
        if key == "provider":
            for name, body in table(value, key).items():
                if not _PROVIDER_NAME.fullmatch(name):
                    raise ConfigError(f"provider {name!r} in config file {path}: {_PROVIDER_RULE}")
                for k, v in table(body, f"provider.{name}").items():
                    if k in provider_keys:
                        values[f"{_PROVIDER_PREFIX}{name}_{k}".upper()] = text(
                            v, f"provider.{name}.{k}"
                        )
                    else:
                        unknown.append(f"provider.{name}.{k}")
        elif key in _SECTIONS:
            known = {f.name for f in fields(_SECTIONS[key])}
            for k, v in table(value, key).items():
                if k in known:
                    values[_env_name(key, k)] = text(v, f"{key}.{k}")
                else:
                    unknown.append(f"{key}.{k}")
        elif key in top_level:
            values[_env_name(None, key)] = text(value, key)
        else:
            unknown.append(key)
    if unknown:
        raise ConfigError(f"unknown keys in config file {path}: {', '.join(sorted(unknown))}")
    return values


def _read(f: Field[Any], name: str, env: Mapping[str, str]) -> Any:
    """One field's value from `env`, by the type it is declared with. A required field that is
    absent was reported as missing before this is reached."""
    default: Any = f.default
    if f.type == "int":
        return _positive_int(env, name, default)
    if f.type == "bool":
        return _bool(env, name, default)
    if f.type == "float":
        return _non_negative_float(env, name, default)
    if name not in env:
        return default
    value = env[name]
    if "norm" in f.metadata:
        value = f.metadata["norm"](value)
    if "choices" in f.metadata and value not in f.metadata["choices"]:
        options = " or ".join(f.metadata["choices"])
        raise ConfigError(f"{name} must be {options}, got {value!r}")
    return Path(value) if f.type == "Path" else value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value < 1:
        raise ConfigError(f"{name} must be at least 1, got {value}")
    return value


def _non_negative_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    # `nan` passes float() and fails every comparison, so it would pass a `< 0` check.
    if not value >= 0 or value == float("inf"):
        raise ConfigError(f"{name} must be a finite number, 0 or more, got {raw!r}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").lower()
    if not raw:
        return default
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def apply_environment() -> None:
    """Turn off third-party telemetry.

    Safe to call any time before the first model or embedding call. Nothing here is read at import
    time. It used to export OPENAI_* as well, because litellm read those behind our back; models and
    the encoder are given their key and base explicitly, so nothing needs them in the environment.
    """
    # No telemetry leaves this service. huggingface_hub honours both of these. dspy 2.4.9 and the
    # rest of the tree ship no telemetry, so nothing else is set here.
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
