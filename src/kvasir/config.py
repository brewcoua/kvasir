"""Configuration parsing. The only place in the service that reads os.environ.

Two sources, each setting in exactly one. The environment holds what a deployment fixes and what is
secret: the token, the data directory, the address, thread width, logging and the model providers.
Everything else is stored in `kvasir.db` (`kvasir.settings_store`) and edited through the API; a
field with nothing stored has its default.

An environment variable is `KVASIR_<KEY>` for a top-level field and `KVASIR_LOG_<KEY>` for logging.
A provider is `KVASIR_PROVIDER_<NAME>_API_BASE` and `KVASIR_PROVIDER_<NAME>_API_KEY`. A stored field
is keyed by its path, `<section>.<key>`, so `research.max_searches`.

A bad environment stops the start. A bad stored value is logged and its default used instead, since
the API that would fix it must still come up. `strict=True` raises instead, for checking a change
before it is stored. The models and the search URL have no default: until they are stored the
service runs, and `Settings.missing` names them.

Every provider is an OpenAI-compatible endpoint. A model is written `<provider>/<name>` and
`Settings.resolve` turns it into what dspy needs, with the endpoint and key given explicitly;
nothing exports them into the environment.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import MISSING, Field, dataclass, field, fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from kvasir.settings_store import SettingsStore
from kvasir.storm.runtime import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_HOST_INTERVAL_SECONDS,
    DEFAULT_MAX_THREADS,
)

logger = logging.getLogger("kvasir")

# The OpenAI client refuses an empty key, and Ollama ignores whatever it is sent.
NO_API_KEY = "none"

_PROVIDER_NAME = re.compile(r"[a-z0-9_-]+")
_PROVIDER_PREFIX = "KVASIR_PROVIDER_"
_PROVIDER_RULE = "names are lowercase letters, digits, _ and -"


class ConfigError(Exception):
    """Configuration is missing or unusable. Raised before the server starts serving."""


class SetupRequired(Exception):
    """Something that calls a model or searches, asked for before the settings it needs."""


def _choice(*options: str) -> dict[str, Any]:
    return {"norm": str.lower, "choices": options}


def _help(text: str, **extra: Any) -> dict[str, Any]:
    """A stored field's metadata: `text` is what the settings page shows under it."""
    return {"help": text} | extra


# A stored field the service cannot run without. Its default, empty, means unset.
_REQUIRED = {"required": True}
# A stored field read once at startup, so a change applies at the next.
_RESTART = {"restart": True}


@dataclass(frozen=True, slots=True)
class Provider:
    """An OpenAI-compatible endpoint models are served from."""

    api_base: str
    api_key: str = field(default=NO_API_KEY, repr=False)


@dataclass(frozen=True, slots=True)
class Model:
    """A model reference resolved against its provider, as the clients are given it."""

    # Prefixed `openai/` because dspy routes on the leading segment and consumes it; litellm then
    # reaches the endpoint through its OpenAI client, which is what every provider speaks.
    model: str
    api_base: str
    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelSettings:
    """Models are `<provider>/<name>`, the provider being one named in the environment."""

    fast: str = field(
        default="",
        metadata=_help(
            "Conversation simulation, question asking, polishing, and research intake.",
            **_REQUIRED,
        ),
    )
    strong: str = field(
        default="",
        metadata=_help(
            "Outline and article generation, and the research outline, report and summary.",
            **_REQUIRED,
        ),
    )
    # The model for judgements, which are worth thinking about. Unset falls back to `strong`.
    reason: str | None = field(
        default=None,
        metadata=_help(
            "Research judgements, and the last word on a doubtful sentence. Unset uses the strong "
            "model."
        ),
    )
    # No default fits every provider, and an embedding model cannot be swapped for another's.
    embedding: str = field(
        default="", metadata=_help("Embedding model, for both modes.", **_REQUIRED)
    )
    # A grounding checker such as MiniCheck, or none. See `kvasir.research.ground`.
    checker: str | None = field(
        default=None,
        metadata=_help(
            "A grounding checker such as MiniCheck. Unset leaves every check to the fast model."
        ),
    )
    # Sent only when set. Unset leaves the gateway's default, which for a thinking model
    # served by Ollama spends each role's token budget on reasoning before any answer.
    reasoning_effort: str | None = field(
        default=None,
        metadata=_help(
            "Sent as reasoning_effort with every call but the reason model's. none stops a "
            "thinking model on Ollama spending its budget on reasoning."
        ),
    )
    # Sent to the reason tier instead of `reasoning_effort`: that tier is meant to think, and its
    # token budgets leave room for it.
    reason_effort: str | None = field(
        default="medium", metadata=_help("Sent as reasoning_effort to the reason model.")
    )
    # The context window the models are served with, such as Ollama's `num_ctx`. It sizes
    # how much of a page one intake call reads, the room left for its answer, and a section's.
    context_tokens: int = field(
        default=DEFAULT_CONTEXT_TOKENS,
        metadata=_help(
            "The context window the models are served with, such as Ollama's num_ctx, not the "
            "model's maximum."
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchSettings:
    url: str = field(
        default="",
        metadata=_help("SearXNG base URL.", norm=lambda value: value.rstrip("/"), **_REQUIRED),
    )
    top_k: int = field(default=3, metadata=_help("Search results per query."))


@dataclass(frozen=True, slots=True, kw_only=True)
class RunSettings:
    # Memory by default: a client such as Open WebUI keeps its own record of every answer,
    # so history across restarts is for debugging the service itself.
    store: str = field(
        default="memory",
        metadata=_help(
            "sqlite keeps runs across restarts.", **_choice("memory", "sqlite"), **_RESTART
        ),
    )
    max: int = field(default=100, metadata=_help("Runs kept, most recent first.", **_RESTART))
    ttl_days: int = field(
        default=30,
        metadata=_help("Age past which a stored run is deleted. sqlite only.", **_RESTART),
    )
    # Off by default: prompts carry the pages read and the user's question, which some
    # deployments must not retain. Timing and tokens are kept either way.
    keep_call_content: bool = field(
        default=False,
        metadata=_help(
            "Keep each model call's messages and answer, and each search's results, with its run.",
            **_RESTART,
        ),
    )
    max_concurrent: int = field(
        default=1, metadata=_help("Concurrent runs. Beyond this, a request gets 429.")
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionSettings:
    ttl_hours: int = field(
        default=168, metadata=_help("Session expiry, applied at startup.", **_RESTART)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ResearchSettings:
    """The deep research pipeline's defaults, which a request overrides one by one. Searches
    are the budget: each costs about nine page reads."""

    max_searches: int = field(
        default=16, metadata=_help("Searches in a run, before the 8 for empty cells.")
    )
    results_per_query: int = field(
        default=3, metadata=_help("Pages read per search by the ledger.")
    )
    max_attempts: int = field(
        default=4, metadata=_help("Queries tried on one question before it is given up.")
    )
    follow_ups: bool = field(
        default=True, metadata=_help("Whether an answered question can add harder ones.")
    )
    max_depth: int = field(
        default=2, metadata=_help("Levels of follow-ups below the first questions.")
    )
    max_entities: int = field(
        default=8, metadata=_help("Options a ranking or table compares, when the run chooses them.")
    )
    max_criteria: int = field(default=5, metadata=_help("Criteria, when the run chooses them."))


@dataclass(frozen=True, slots=True, kw_only=True)
class StormSettings:
    max_conv_turn: int = field(default=3, metadata=_help("Questions per perspective."))
    max_perspective: int = field(default=3, metadata=_help("Perspectives to research."))


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
    contact: str = field(
        default="", metadata=_help("Contact named in the user agent of page requests.")
    )
    # Between the starts of two requests to one host. 0 turns the wait off.
    host_interval_seconds: float = field(
        default=DEFAULT_HOST_INTERVAL_SECONDS,
        metadata=_help("Least time between the starts of two page requests to one host."),
    )
    # Whether a page that needs JavaScript is rendered in a browser. Needs the browser extra.
    render: str = field(
        default="auto",
        metadata=_help(
            "auto renders a page that needs JavaScript, with the browser extra installed.",
            **_choice("auto", "off"),
        ),
    )
    # Renders through Patchright, which hides the signs of automation. Getting past a challenge can
    # break a site's terms, so it is the operator's call and off by default.
    stealth: bool = field(
        default=False,
        metadata=_help(
            "Render through Patchright, which hides the signs of automation. Getting past a "
            "challenge can break a site's terms."
        ),
    )
    # How long a cached page is served without asking its site, then it is revalidated. 0 fetches
    # every page again.
    max_age_hours: int = field(
        default=48,
        metadata=_help(
            "Hours a cached page is served without asking its site. 0 fetches every page again.",
            min=0,
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FaviconSettings:
    # Tried in order, the first icon found wins. Empty turns favicons off. `direct` asks the site
    # itself; the others hand the site's name to a third party.
    providers: tuple[str, ...] = field(
        default=("direct",),
        metadata=_help(
            "Where site icons come from, tried in order. Empty turns favicons off. All but direct "
            "learn each site looked up.",
            **_choice("direct", "duckduckgo", "google", "custom"),
        ),
    )
    # Used by the `custom` provider: a URL with `{domain}` where the site's host goes.
    custom_url: str = field(
        default="",
        metadata=_help("URL for the custom provider, with {domain} where the site's host goes."),
    )
    # How long a found icon is served, and how long a failed lookup is remembered.
    ttl_days: int = field(
        default=30, metadata=_help("Days a found icon is served before it is looked up again.")
    )
    miss_ttl_days: int = field(default=3, metadata=_help("Days a site with no icon is remembered."))


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceSettings:
    # Off by default: classifying a site asks Wikidata about the domains of search results.
    enabled: bool = field(
        default=False,
        metadata=_help("Classify the sites of search results by kind and trust tier."),
    )
    # Whether Wikidata is asked about hosts no rule, mark or list decides.
    wikidata: bool = field(
        default=True,
        metadata=_help(
            "Ask Wikidata about hosts no rule, mark or list decides. Off sends no domain anywhere."
        ),
    )
    endpoint: str = field(
        default="https://query.wikidata.org/sparql", metadata=_help("SPARQL endpoint asked.")
    )
    # How long a Wikidata answer is kept, and how long a host it does not know is remembered. Also
    # the age at which a downloaded list is fetched again.
    ttl_days: int = field(
        default=30,
        metadata=_help(
            "Days a Wikidata answer is kept, and a downloaded list served before it is fetched "
            "again."
        ),
    )
    miss_ttl_days: int = field(
        default=7, metadata=_help("Days a host Wikidata does not know is remembered.")
    )
    # Whether Wikipedia's perennial sources list (WP:RSP) tags hosts `rsp_reliable`, `rsp_mixed`,
    # `rsp_unreliable` or `rsp_deprecated`. Fetched with the lists; needs `enabled`.
    rsp: bool = field(
        default=False, metadata=_help("Also tag hosts by Wikipedia's perennial sources list.")
    )
    # Hosts, and their subdomains, that are always trusted or never read. They beat any mark.
    allow: tuple[str, ...] = field(
        default=(), metadata=_help("Hosts, with their subdomains, always trusted. Beats any mark.")
    )
    deny: tuple[str, ...] = field(
        default=(),
        metadata=_help(
            "Hosts, with their subdomains, never read. Beats sources.allow and any mark."
        ),
    )
    # Lists fetched into data_dir, each as `<label>=<url>`. The label is a kind (`government`) or a
    # tag (`ai_generated`) given to every host on the list. Hosts files, uBlacklist and CSV with
    # the host first are read.
    lists: tuple[str, ...] = field(
        default=(
            "government=https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-full.csv",
        ),
        metadata=_help("Lists to download, each as <label>=<url>, the label a kind or a tag."),
    )


# The sections stored in kvasir.db, keyed by the Settings field.
_STORED: dict[str, Any] = {
    "model": ModelSettings,
    "search": SearchSettings,
    "runs": RunSettings,
    "sessions": SessionSettings,
    "research": ResearchSettings,
    "storm": StormSettings,
    "fetch": FetchSettings,
    "favicons": FaviconSettings,
    "sources": SourceSettings,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """Resolved configuration. Build with `Settings.load()`."""

    data_dir: Path = Path("/data")
    # Every route but health, the login and the page needs it. See `kvasir.api.auth`.
    token: str = field(repr=False)
    # Inside the container, which is where the port is published from.
    host: str = "0.0.0.0"
    port: int = 8080
    # How wide each of the pipeline's thread pools runs, and how many search or page
    # requests may be in flight at once across the whole process.
    max_threads: int = DEFAULT_MAX_THREADS
    providers: Mapping[str, Provider]
    log: LogSettings
    model: ModelSettings
    search: SearchSettings
    runs: RunSettings
    sessions: SessionSettings
    research: ResearchSettings
    storm: StormSettings
    fetch: FetchSettings
    favicons: FaviconSettings
    sources: SourceSettings

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def db(self) -> Path:
        """The stored settings, and the runs when `runs.store` is sqlite."""
        return self.data_dir / "kvasir.db"

    @property
    def pages_db(self) -> Path:
        return self.data_dir / "pages.db"

    @property
    def favicons_db(self) -> Path:
        return self.data_dir / "favicons.db"

    @property
    def sources_db(self) -> Path:
        return self.data_dir / "sources.db"

    @property
    def missing(self) -> tuple[str, ...]:
        """The stored fields the service needs before it can run anything, still unset."""
        return tuple(
            key for key, f in STORED_FIELDS if f.metadata.get("required") and not self.value(key)
        )

    def value(self, key: str) -> Any:
        """The stored field `key`, such as `research.max_searches`."""
        section, _, name = key.partition(".")
        return getattr(getattr(self, section), name)

    def ready(self) -> None:
        """Raises SetupRequired while `missing` names anything."""
        if self.missing:
            raise SetupRequired(", ".join(self.missing))

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
    def load(
        cls,
        env: Mapping[str, str] | None = None,
        stored: Mapping[str, Any] | None = None,
        *,
        strict: bool = False,
    ) -> Settings:
        """Resolve `env`, defaulting to os.environ, and `stored`, defaulting to what kvasir.db in
        the data directory holds. `strict` raises on a bad stored value rather than dropping it."""
        env = {
            k: v.strip()
            for k, v in (os.environ if env is None else env).items()
            if k.startswith("KVASIR_") and v.strip()
        }
        raw_providers = _raw_providers(env)
        missing = [
            f"{_PROVIDER_PREFIX}{name.upper()}_API_BASE"
            for name, values in raw_providers.items()
            if "api_base" not in values
        ]
        missing += [name for f, name in _ENV_FIELDS if f.default is MISSING and name not in env]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")
        unread = sorted(
            name for name in env if name not in _ENV_NAMES and not name.startswith(_PROVIDER_PREFIX)
        )
        if unread:
            logger.warning(
                "not read from the environment, set them in the settings instead: %s",
                ", ".join(unread),
            )

        top = {f.name: _read_env(f, name, env) for f, name in _ENV_FIELDS if f.name in _TOP_LEVEL}
        log = LogSettings(
            **{f.name: _read_env(f, name, env) for f, name in _ENV_FIELDS if f.name not in top}
        )
        providers = {
            name: Provider(
                api_base=values["api_base"].rstrip("/"),
                api_key=values.get("api_key", NO_API_KEY),
            )
            for name, values in raw_providers.items()
        }
        defaults = {section: section_cls() for section, section_cls in _STORED.items()}
        base = cls(providers=MappingProxyType(providers), log=log, **top, **defaults)

        if stored is None:
            # Read without creating, so a CLI on another host does not leave a database behind.
            rows = SettingsStore(base.db).load() if base.db.exists() else {}
            stored = {}
            for key, text in rows.items():
                try:
                    stored[key] = json.loads(text)
                except ValueError:
                    _problem(f"{key} is not JSON: {text!r}", strict)
        return base.with_stored(stored, strict=strict)

    def with_stored(self, stored: Mapping[str, Any], *, strict: bool = False) -> Settings:
        """These settings' environment with `stored` in place of every stored field. A field not in
        `stored` has its default."""
        for key in sorted(set(stored) - {key for key, _ in STORED_FIELDS}):
            _problem(f"unknown setting {key}", strict)
        sections = {}
        for section, section_cls in _STORED.items():
            values = {}
            for f in fields(section_cls):
                key = f"{section}.{f.name}"
                if key in stored:
                    try:
                        values[f.name] = _parse(f, key, stored[key])
                    except ConfigError as exc:
                        _problem(str(exc), strict)
            sections[section] = section_cls(**values)
        settings = replace(self, **sections)

        # A provider dropped from the environment leaves its models unset rather than failing the
        # first run that uses one.
        model = settings.model
        for f in fields(ModelSettings):
            ref = getattr(model, f.name)
            if f"model.{f.name}" in MODEL_KEYS and ref:
                try:
                    settings.resolve(ref)
                except ConfigError as exc:
                    _problem(f"model.{f.name}: {exc}", strict)
                    default: Any = f.default
                    model = replace(model, **{f.name: default})
        for section, check in (("favicons", _check_favicons), ("sources", _check_sources)):
            try:
                check(getattr(settings, section))
            except ConfigError as exc:
                _problem(str(exc), strict)
                sections[section] = _STORED[section]()
        return replace(
            settings, model=model, favicons=sections["favicons"], sources=sections["sources"]
        )


def _problem(message: str, strict: bool) -> None:
    """A stored value that cannot be used: an error when `strict`, otherwise logged and dropped."""
    if strict:
        raise ConfigError(message)
    logger.warning("stored setting ignored: %s", message)


# The stored fields that name a model as `<provider>/<name>`.
MODEL_KEYS = frozenset(
    f"model.{name}" for name in ("fast", "strong", "reason", "checker", "embedding")
)
_TOP_LEVEL = frozenset(f.name for f in fields(Settings)) - {"providers", "log"} - set(_STORED)
# Each field read from the environment, with its variable.
_ENV_FIELDS: tuple[tuple[Field[Any], str], ...] = tuple(
    (f, f"KVASIR_{f.name}".upper()) for f in fields(Settings) if f.name in _TOP_LEVEL
) + tuple((f, f"KVASIR_LOG_{f.name}".upper()) for f in fields(LogSettings))
_ENV_NAMES = frozenset(name for _, name in _ENV_FIELDS)
# Every stored field, by its key.
STORED_FIELDS: tuple[tuple[str, Field[Any]], ...] = tuple(
    (f"{section}.{f.name}", f)
    for section, section_cls in _STORED.items()
    for f in fields(section_cls)
)


def _check_favicons(favicons: FaviconSettings) -> None:
    if "custom" not in favicons.providers:
        return
    url = urlsplit(favicons.custom_url.replace("{domain}", "example.com"))
    if (
        "{domain}" not in favicons.custom_url
        or url.scheme not in ("http", "https")
        or not url.netloc
    ):
        raise ConfigError(
            "favicons.custom_url must be an http(s) URL containing {domain} "
            f"when the custom provider is used, got {favicons.custom_url!r}"
        )


def _check_sources(sources: SourceSettings) -> None:
    if not sources.enabled:
        return
    # Imported here because the research package imports this module.
    from kvasir.research.favicons import normalize_host
    from kvasir.research.sources import LIST_LABELS

    def check_url(name: str, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError(f"sources.{name} must be an http(s) URL, got {url!r}")

    check_url("endpoint", sources.endpoint)
    for entry in sources.lists:
        label, _, url = entry.partition("=")
        if label not in LIST_LABELS:
            raise ConfigError(
                f"sources.lists entry {entry!r} must be <label>=<url> with a label of "
                f"{', '.join(sorted(LIST_LABELS))}"
            )
        check_url("lists", url)
    for name, hosts in (("allow", sources.allow), ("deny", sources.deny)):
        for host in hosts:
            if normalize_host(host.removesuffix(".")) is None:
                raise ConfigError(f"sources.{name} holds {host!r}, which is not a DNS name")


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


def _read_env(f: Field[Any], name: str, env: Mapping[str, str]) -> Any:
    """One field's value from `env`, by the type it is declared with. A required field that is
    absent was reported as missing before this is reached."""
    if name not in env:
        return f.default
    raw = env[name]
    if f.type == "int":
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
        if value < 1:
            raise ConfigError(f"{name} must be at least 1, got {value}")
        return value
    return Path(raw) if f.type == "Path" else _text(f, name, raw)


def _text(f: Field[Any], name: str, value: str) -> str:
    if "norm" in f.metadata:
        value = f.metadata["norm"](value)
    if "choices" in f.metadata and value not in f.metadata["choices"]:
        options = " or ".join(f.metadata["choices"])
        raise ConfigError(f"{name} must be {options}, got {value!r}")
    return value


def _parse(f: Field[Any], key: str, value: Any) -> Any:
    """One stored field's JSON value, checked against the type it is declared with."""
    if f.type == "int":
        least = f.metadata.get("min", 1)
        # bool is an int to Python, and true is no count.
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"{key} must be an integer, got {value!r}")
        if value < least:
            raise ConfigError(f"{key} must be at least {least}, got {value}")
        return value
    if f.type == "float":
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ConfigError(f"{key} must be a number, got {value!r}")
        if not math.isfinite(value) or value < 0:
            raise ConfigError(f"{key} must be a finite number, 0 or more, got {value!r}")
        return float(value)
    if f.type == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{key} must be true or false, got {value!r}")
        return value
    if f.type == "tuple[str, ...]":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"{key} must be a list of strings, got {value!r}")
        items = [item.strip() for item in value if item.strip()]
        if "choices" not in f.metadata:
            return tuple(items)
        items = [item.lower() for item in items]
        unknown = [item for item in items if item not in f.metadata["choices"]]
        if unknown:
            options = ", ".join(f.metadata["choices"])
            raise ConfigError(
                f"{key} holds {', '.join(map(repr, unknown))}; each must be one of {options}"
            )
        return tuple(items)
    if f.type == "str | None" and (value is None or value == ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string, got {value!r}")
    return _text(f, key, value.strip())


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
