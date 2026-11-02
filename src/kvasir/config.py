"""Configuration parsing. The only place in the service that reads os.environ or the config file.

Configuration is environment variables, optionally backed by a TOML file whose keys are the
`Settings` field names. A variable that is set wins over the file. Missing required values raise at
startup rather than on the first request.

OPENAI_API_KEY and OPENAI_API_BASE keep those names because they are the conventional spelling for
an OpenAI-compatible endpoint, which is what the gateway serves. They are read here and passed
explicitly to the models and the encoder; nothing exports them back into the environment.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from kvasir.storm.runtime import DEFAULT_CONTEXT_TOKENS, DEFAULT_MAX_THREADS

DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"

_REQUIRED = (
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "KVASIR_MODEL_FAST",
    "KVASIR_MODEL_STRONG",
    "KVASIR_SEARXNG_URL",
)


# Read when KVASIR_CONFIG is unset, if it exists, so a local start needs no variable at all.
DEFAULT_CONFIG_FILE = Path("kvasir.toml")

# Keep their conventional names; every other setting is KVASIR_ followed by its field name.
_UNPREFIXED = ("OPENAI_API_KEY", "OPENAI_API_BASE", "LOG_LEVEL", "LOG_FORMAT")


class ConfigError(Exception):
    """Configuration is missing or unusable. Raised before the server starts serving."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved configuration. Build with `Settings.from_env()`."""

    openai_api_key: str
    openai_api_base: str
    model_fast: str
    model_strong: str
    # The model research reports are outlined and written with. None: the strong model.
    model_writer: str | None
    embedding_model: str
    # A grounding checker such as MiniCheck, or none. See `kvasir.research.ground`.
    model_checker: str | None
    searxng_url: str
    data_dir: Path
    session_ttl_hours: int
    run_store: str
    max_runs: int
    run_ttl_days: int
    keep_call_content: bool
    max_concurrent_runs: int
    max_threads: int
    context_tokens: int
    search_top_k: int
    max_conv_turn: int
    max_perspective: int
    research_max_searches: int
    research_results_per_query: int
    research_max_attempts: int
    research_follow_ups: bool
    research_max_depth: int
    research_max_entities: int
    research_max_criteria: int
    log_level: str
    log_format: str
    reasoning_effort: str | None

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def runs_db(self) -> Path:
        return self.data_dir / "kvasir.db"

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
        if path is not None:
            env = _read_file(Path(path)) | {k: v for k, v in env.items() if v.strip()}

        missing = [name for name in _REQUIRED if not env.get(name, "").strip()]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")

        return cls(
            openai_api_key=env["OPENAI_API_KEY"].strip(),
            openai_api_base=env["OPENAI_API_BASE"].strip().rstrip("/"),
            model_fast=_model(env, "KVASIR_MODEL_FAST"),
            model_strong=_model(env, "KVASIR_MODEL_STRONG"),
            model_writer=(
                _model(env, "KVASIR_MODEL_WRITER")
                if env.get("KVASIR_MODEL_WRITER", "").strip()
                else None
            ),
            embedding_model=(
                _model(env, "KVASIR_EMBEDDING_MODEL")
                if env.get("KVASIR_EMBEDDING_MODEL", "").strip()
                else DEFAULT_EMBEDDING_MODEL
            ),
            model_checker=(
                _model(env, "KVASIR_MODEL_CHECKER")
                if env.get("KVASIR_MODEL_CHECKER", "").strip()
                else None
            ),
            searxng_url=env["KVASIR_SEARXNG_URL"].strip().rstrip("/"),
            data_dir=Path(env.get("KVASIR_DATA_DIR", "").strip() or "/data"),
            session_ttl_hours=_positive_int(env, "KVASIR_SESSION_TTL_HOURS", 168),
            # Memory by default: a client such as Open WebUI keeps its own record of every answer,
            # so history across restarts is for debugging the service itself.
            run_store=_run_store(env),
            max_runs=_positive_int(env, "KVASIR_MAX_RUNS", 100),
            run_ttl_days=_positive_int(env, "KVASIR_RUN_TTL_DAYS", 30),
            # Off by default: prompts carry the pages read and the user's question, which some
            # deployments must not retain. Timing and tokens are kept either way.
            keep_call_content=_bool(env, "KVASIR_KEEP_CALL_CONTENT", False),
            max_concurrent_runs=_positive_int(env, "KVASIR_MAX_CONCURRENT_RUNS", 1),
            # How wide each of the pipeline's thread pools runs, and how many search or page
            # requests may be in flight at once across the whole process.
            max_threads=_positive_int(env, "KVASIR_MAX_THREADS", DEFAULT_MAX_THREADS),
            # The context window the models are served with, such as Ollama's `num_ctx`. It sizes
            # how much of a page one intake call reads, and the room left for its answer.
            context_tokens=_positive_int(env, "KVASIR_CONTEXT_TOKENS", DEFAULT_CONTEXT_TOKENS),
            search_top_k=_positive_int(env, "KVASIR_SEARCH_TOP_K", 3),
            max_conv_turn=_positive_int(env, "KVASIR_MAX_CONV_TURN", 3),
            max_perspective=_positive_int(env, "KVASIR_MAX_PERSPECTIVE", 3),
            # The deep research pipeline's defaults, which a request overrides one by one. Searches
            # are the budget: each costs about nine page reads.
            research_max_searches=_positive_int(env, "KVASIR_RESEARCH_MAX_SEARCHES", 16),
            research_results_per_query=_positive_int(env, "KVASIR_RESEARCH_RESULTS_PER_QUERY", 3),
            research_max_attempts=_positive_int(env, "KVASIR_RESEARCH_MAX_ATTEMPTS", 4),
            research_follow_ups=_bool(env, "KVASIR_RESEARCH_FOLLOW_UPS", True),
            research_max_depth=_positive_int(env, "KVASIR_RESEARCH_MAX_DEPTH", 2),
            research_max_entities=_positive_int(env, "KVASIR_RESEARCH_MAX_ENTITIES", 8),
            research_max_criteria=_positive_int(env, "KVASIR_RESEARCH_MAX_CRITERIA", 5),
            log_level=(env.get("LOG_LEVEL", "").strip() or "INFO").upper(),
            # JSON by default: this runs as a container whose stdout goes to a log shipper, and
            # a run's lines interleave across threads, so they need to be parseable rather than
            # readable. `text` is for reading them by eye during development.
            log_format=_log_format(env),
            # Sent only when set. Unset leaves the gateway's default, which for a thinking model
            # served by Ollama spends each role's token budget on reasoning before any answer.
            reasoning_effort=env.get("KVASIR_REASONING_EFFORT", "").strip() or None,
        )


def _env_name(key: str) -> str:
    name = key.upper()
    return name if name in _UNPREFIXED else f"KVASIR_{name}"


def _read_file(path: Path) -> dict[str, str]:
    """The config file as the environment variables it stands for."""
    try:
        data = tomllib.loads(path.read_text())
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid TOML: {exc}") from None

    known = {field.name for field in fields(Settings)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown keys in config file {path}: {', '.join(unknown)}")

    values = {}
    for key, value in data.items():
        if isinstance(value, bool):
            values[_env_name(key)] = "true" if value else "false"
        elif isinstance(value, str | int):
            values[_env_name(key)] = str(value)
        else:
            raise ConfigError(
                f"{key} in config file {path} must be a string, an integer or a boolean"
            )
    return values


def _model(env: Mapping[str, str], name: str) -> str:
    """A model name, checked for the provider prefix dspy requires. Language models and the
    embedding model alike: both are reached through dspy.

    dspy routes on the leading `/`-separated segment and consumes it, so `openai/gpt-4o-mini`
    reaches the gateway as `gpt-4o-mini`, and everything after that first segment is passed through
    untouched: `openai/ollama/model:cloud` asks the gateway for `ollama/model:cloud`. A name without
    a prefix is rejected here rather than silently prefixed, because a name being rewritten out of
    sight is exactly what makes this confusing.
    """
    value = env[name].strip()
    if "/" not in value:
        raise ConfigError(
            f"{name} must name a provider, as in openai/{value}: dspy routes on the "
            f"first path segment and sends the rest to the gateway"
        )
    return value


def _log_format(env: Mapping[str, str]) -> str:
    value = (env.get("LOG_FORMAT", "").strip() or "json").lower()
    if value not in ("json", "text"):
        raise ConfigError(f"LOG_FORMAT must be json or text, got {value!r}")
    return value


def _run_store(env: Mapping[str, str]) -> str:
    value = (env.get("KVASIR_RUN_STORE", "").strip() or "memory").lower()
    if value not in ("memory", "sqlite"):
        raise ConfigError(f"KVASIR_RUN_STORE must be memory or sqlite, got {value!r}")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value < 1:
        raise ConfigError(f"{name} must be at least 1, got {value}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
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
