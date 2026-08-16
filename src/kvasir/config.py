"""Environment parsing. The only place in the service that reads os.environ.

All configuration is environment variables, and missing required values raise at startup rather
than on the first request.

OPENAI_API_KEY and OPENAI_API_BASE keep those names because they are the conventional spelling for
an OpenAI-compatible endpoint, which is what the gateway serves. They are read here and passed
explicitly to the models and the encoder; nothing exports them back into the environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kvasir.storm.runtime import DEFAULT_MAX_THREADS

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

_REQUIRED = (
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "KVASIR_MODEL_FAST",
    "KVASIR_MODEL_STRONG",
    "KVASIR_SEARXNG_URL",
)


class ConfigError(Exception):
    """Configuration is missing or unusable. Raised before the server starts serving."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved configuration. Build with `Settings.from_env()`."""

    openai_api_key: str
    openai_api_base: str
    model_fast: str
    model_strong: str
    embedding_model: str
    searxng_url: str
    data_dir: Path
    session_ttl_hours: int
    max_concurrent_runs: int
    max_threads: int
    search_top_k: int
    max_conv_turn: int
    max_perspective: int
    log_level: str
    log_format: str

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        missing = [name for name in _REQUIRED if not env.get(name, "").strip()]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")

        return cls(
            openai_api_key=env["OPENAI_API_KEY"].strip(),
            openai_api_base=env["OPENAI_API_BASE"].strip().rstrip("/"),
            # Model names carry a routing prefix and must reach the gateway verbatim. Nothing here
            # normalises, validates or strips them.
            model_fast=env["KVASIR_MODEL_FAST"].strip(),
            model_strong=env["KVASIR_MODEL_STRONG"].strip(),
            embedding_model=env.get("KVASIR_EMBEDDING_MODEL", "").strip()
            or DEFAULT_EMBEDDING_MODEL,
            searxng_url=env["KVASIR_SEARXNG_URL"].strip().rstrip("/"),
            data_dir=Path(env.get("KVASIR_DATA_DIR", "").strip() or "/data"),
            session_ttl_hours=_positive_int(env, "KVASIR_SESSION_TTL_HOURS", 168),
            max_concurrent_runs=_positive_int(env, "KVASIR_MAX_CONCURRENT_RUNS", 1),
            # How wide each of the pipeline's thread pools runs, and how many search or page
            # requests may be in flight at once across the whole process.
            max_threads=_positive_int(env, "KVASIR_MAX_THREADS", DEFAULT_MAX_THREADS),
            search_top_k=_positive_int(env, "KVASIR_SEARCH_TOP_K", 3),
            max_conv_turn=_positive_int(env, "KVASIR_MAX_CONV_TURN", 3),
            max_perspective=_positive_int(env, "KVASIR_MAX_PERSPECTIVE", 3),
            log_level=(env.get("LOG_LEVEL", "").strip() or "INFO").upper(),
            # JSON by default: this runs as a container whose stdout goes to a log shipper, and
            # a run's lines interleave across threads, so they need to be parseable rather than
            # readable. `text` is for reading them by eye during development.
            log_format=_log_format(env),
        )


def _log_format(env: Mapping[str, str]) -> str:
    value = (env.get("LOG_FORMAT", "").strip() or "json").lower()
    if value not in ("json", "text"):
        raise ConfigError(f"LOG_FORMAT must be json or text, got {value!r}")
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
