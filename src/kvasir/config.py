"""Environment parsing. The only place in the service that reads os.environ.

All configuration is environment variables, and missing required values raise at startup rather
than on the first request.

The OPENAI_* names are not ours to choose. litellm and the fork's Encoder read them
directly, and pointing embeddings at the gateway depends on that, so there is deliberately no
KVASIR_ alias for them: an alias is how the encoder silently ends up on api.openai.com.
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

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def cache_dir(self) -> Path:
        """Where litellm caches model and embedding responses.

        Under the data directory rather than $HOME, so it survives a restart and needs no writable
        home. Upstream opened it under Path.home() while being imported, which is what the image
        used to work around by pointing HOME at /tmp.
        """
        return self.data_dir / "cache"

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
        )


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


def apply_environment(settings: Settings) -> None:
    """Set the environment that the fork and litellm read behind our back.

    Safe to call any time before the first model or embedding call. Nothing here is read at import
    time.
    """
    # Both models and embeddings are given the key and base explicitly now, so nothing depends on
    # these to reach the gateway. They stay because litellm reads them for anything constructed
    # without them, and a stray default pointing at api.openai.com is the failure they prevent.
    # OPENAI_BASE_URL mirrors OPENAI_API_BASE because litellm reads both depending on path.
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key
    os.environ["OPENAI_API_BASE"] = settings.openai_api_base
    os.environ["OPENAI_BASE_URL"] = settings.openai_api_base

    # No telemetry leaves this service. huggingface_hub honours both of these; litellm has no
    # environment switch and is turned off in kvasir.storm.runtime. dspy 2.4.9 and the rest of the
    # tree ship no telemetry, so nothing else is set here.
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
