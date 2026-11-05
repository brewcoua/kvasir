"""Writes the golden data the Rust port is compared with. Python stays the reference until cutover.

Run it from the repository root, with a data directory a real service has used:

    uv run python tests/golden/capture.py --data data

Into `tests/golden/`, to commit: `signatures.json`, `urls.json`, `settings.json`, and
`schemas/`. Into `<data>/golden/`, which is not committed because it holds what was researched:
`urls.json` and `runs.json` taken from the databases.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import inspect
import json
import logging
import pkgutil
import sqlite3
import types
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import dspy
from pydantic import BaseModel

import kvasir.storm.interface  # noqa: F401  imported first: the fork's modules import circularly
from kvasir.config import ConfigError, Settings
from kvasir.research.fetch import normalize

GOLDEN = Path(__file__).parent
PACKAGES = ("kvasir.storm", "kvasir.research", "kvasir.methods")
DATABASES = ("kvasir", "pages", "favicons", "sources")
# The ones called through `dspy.ChainOfThought`, which adds a field and so changes the prompt.
CHAIN_OF_THOUGHT = {"AskQuestion", "AskQuestionWithPersona", "FindRelatedTopic", "GenPersona"}
SPELLINGS = [
    "https://example.org",
    "https://example.org/",
    "http://example.org/a",
    "https://www.example.org/a/",
    "https://WWW.Example.ORG/A",
    "https://example.org:443/a",
    "http://example.org:80/a",
    "https://example.org:8443/a",
    "https://example.org/a/index.html",
    "https://example.org/a/index.htm",
    "https://example.org/a#section",
    "https://example.org/#/route/a",
    "https://example.org/#!/route/a?b=1",
    "https://example.org/a?b=2&a=1",
    "https://example.org/a?utm_source=x&id=1",
    "https://example.org/a?fbclid=x",
    "https://example.org/a?gclid=x&ref=y",
    "https://example.org/a%20b/%C3%A9",
    "https://example.org/é",
    "https://bücher.example/a",
    "https://example.org/a/../b",
    "https://example.org//a//b",
    "https://user:secret@example.org/a",
    "https://[2001:db8::1]/a",
    "https://[bad/a",
    "http://127.0.0.1:8000/a",
    "example.org/a",
    "ftp://example.org/a",
    "mailto:a@example.org",
    "",
]


# What `Settings.load` makes of an environment and stored values: (name, env, stored, strict).
SETTINGS_CASES: list[tuple[str, list[tuple[str, str]], dict[str, Any], bool]] = [
    # (name, env, stored, strict)
    ("minimal", [("KVASIR_TOKEN", "t")], {}, False),
    ("no token", [], {}, False),
    ("blank values are unset", [("KVASIR_TOKEN", "t"), ("KVASIR_PORT", "  ")], {}, False),
    ("values are stripped", [("KVASIR_TOKEN", "  t  "), ("KVASIR_HOST", " ::1 ")], {}, False),
    ("not kvasir", [("KVASIR_TOKEN", "t"), ("TOKEN", "x"), ("kvasir_port", "1")], {}, False),
    ("port", [("KVASIR_TOKEN", "t"), ("KVASIR_PORT", "+81")], {}, False),
    ("port words", [("KVASIR_TOKEN", "t"), ("KVASIR_PORT", "abc")], {}, False),
    ("port zero", [("KVASIR_TOKEN", "t"), ("KVASIR_PORT", "0")], {}, False),
    ("port float", [("KVASIR_TOKEN", "t"), ("KVASIR_PORT", "80.0")], {}, False),
    ("threads underscore", [("KVASIR_TOKEN", "t"), ("KVASIR_MAX_THREADS", "1_0")], {}, False),
    ("threads negative", [("KVASIR_TOKEN", "t"), ("KVASIR_MAX_THREADS", "-3")], {}, False),
    ("data dir", [("KVASIR_TOKEN", "t"), ("KVASIR_DATA_DIR", "/srv/k/")], {}, False),
    (
        "log",
        [("KVASIR_TOKEN", "t"), ("KVASIR_LOG_LEVEL", "debug"), ("KVASIR_LOG_FORMAT", "TEXT")],
        {},
        False,
    ),
    ("log format", [("KVASIR_TOKEN", "t"), ("KVASIR_LOG_FORMAT", "xml")], {}, False),
    (
        "unread",
        [("KVASIR_TOKEN", "t"), ("KVASIR_SEARCH_URL", "http://s"), ("KVASIR_ALPHA", "1")],
        {},
        False,
    ),
    (
        "providers",
        [
            ("KVASIR_TOKEN", "t"),
            ("KVASIR_PROVIDER_GATEWAY_API_BASE", "http://g/v1/"),
            ("KVASIR_PROVIDER_GATEWAY_API_KEY", "k"),
            ("KVASIR_PROVIDER_MY_LOCAL_API_BASE", "http://l:11434/v1"),
        ],
        {},
        False,
    ),
    (
        "provider without base",
        [("KVASIR_PROVIDER_A_API_KEY", "k"), ("KVASIR_PROVIDER_B_API_KEY", "k")],
        {},
        False,
    ),
    (
        "provider bad name",
        [("KVASIR_TOKEN", "t"), ("KVASIR_PROVIDER_A.B_API_BASE", "http://x")],
        {},
        False,
    ),
    (
        "provider other suffix",
        [
            ("KVASIR_TOKEN", "t"),
            ("KVASIR_PROVIDER_A_API_BASE", "http://x"),
            ("KVASIR_PROVIDER_A_TIMEOUT", "3"),
        ],
        {},
        False,
    ),
]
_ENV = [("KVASIR_TOKEN", "t"), ("KVASIR_PROVIDER_GATEWAY_API_BASE", "http://g/v1")]
_STORED = {
    "int as text": {"research.max_searches": "3"},
    "int as bool": {"research.max_searches": True},
    "int as float": {"research.max_searches": 3.0},
    "int below one": {"storm.max_conv_turn": 0},
    "int big": {"runs.max": 123456789012},
    "min zero": {"fetch.max_age_hours": 0},
    "min zero below": {"fetch.max_age_hours": -1},
    "float from int": {"fetch.host_interval_seconds": 2},
    "float": {"fetch.host_interval_seconds": 0.25},
    "float tiny": {"fetch.host_interval_seconds": 1e-7},
    "float negative": {"fetch.host_interval_seconds": -0.5},
    "float text": {"fetch.host_interval_seconds": "1"},
    "float bool": {"fetch.host_interval_seconds": False},
    "bool": {"fetch.stealth": True, "research.follow_ups": False},
    "bool text": {"fetch.stealth": "true"},
    "bool int": {"fetch.stealth": 1},
    "list": {"sources.allow": [" a.example ", "", "b.example"]},
    "list not list": {"sources.allow": "a.example"},
    "list not strings": {"sources.deny": ["a", 1]},
    "list choices": {"favicons.providers": [" Google ", "direct"]},
    "list choices empty": {"favicons.providers": []},
    "list choices unknown": {"favicons.providers": ["bing", "direct", "Yandex"]},
    "text": {"fetch.contact": "  ops@example.org  "},
    "text not string": {"fetch.contact": 5},
    "text list": {"fetch.contact": ["a"]},
    "text dict": {"fetch.contact": {"a": "it's"}},
    "text quotes": {"fetch.contact": 7.5, "search.top_k": 'it\'s "x"\n'},
    "optional empty": {"model.reason_effort": "", "model.reasoning_effort": None},
    "optional set": {"model.reasoning_effort": " none "},
    "choice": {"runs.store": " SQLITE "},
    "choice unknown": {"runs.store": "redis"},
    "url": {"search.url": " http://searx:8080/// "},
    "unknown key": {"sessions.ttl_hours": 168, "a.b": 1},
    "models": {
        "model.fast": "gateway/a/b:c",
        "model.strong": "gateway/s",
        "model.embedding": "gateway/e",
        "search.url": "http://s",
    },
    "model no slash": {"model.fast": "fast"},
    "model no name": {"model.strong": "gateway/"},
    "model unknown provider": {"model.reason": "cloud/x", "model.checker": "gateway/c"},
    "favicons custom": {
        "favicons.providers": ["custom"],
        "favicons.custom_url": "https://i.example/{domain}.png",
    },
    "favicons custom no url": {"favicons.providers": ["custom"], "favicons.ttl_days": 9},
    "favicons custom no domain": {
        "favicons.providers": ["direct", "custom"],
        "favicons.custom_url": "https://i.example/x.png",
    },
    "favicons custom ftp": {
        "favicons.providers": ["custom"],
        "favicons.custom_url": "ftp://i.example/{domain}",
    },
    "sources off unchecked": {"sources.endpoint": "nope", "sources.allow": ["not a host"]},
    "sources": {
        "sources.enabled": True,
        "sources.allow": ["Example.ORG.", "www.a.example:443"],
        "sources.lists": ["academic=https://l.example/a.txt", "predatory=http://l.example/p"],
    },
    "sources endpoint": {
        "sources.enabled": True,
        "sources.endpoint": "query.wikidata.org/sparql",
        "sources.ttl_days": 2,
    },
    "sources list label": {
        "sources.enabled": True,
        "sources.lists": ["unknown=https://l.example/a"],
    },
    "sources list no url": {"sources.enabled": True, "sources.lists": ["government"]},
    "sources list ftp": {"sources.enabled": True, "sources.lists": ["news=ftp://l.example/a"]},
    "sources allow ip": {"sources.enabled": True, "sources.allow": ["1.2.3.4"]},
    "sources deny bad": {"sources.enabled": True, "sources.deny": ["under_score.example"]},
    "sources idn": {
        "sources.enabled": True,
        "sources.deny": ["bücher.example", "xn--bcher-kva.example"],
    },
    "sources one label": {"sources.enabled": True, "sources.deny": ["localhost"]},
    "several problems": {
        "research.max_searches": "x",
        "storm.max_perspective": 0,
        "a.b": 1,
        "model.fast": "nope/x",
    },
}
for _name, _stored in _STORED.items():
    SETTINGS_CASES.append((_name, _ENV, _stored, False))
    SETTINGS_CASES.append((f"{_name}, strict", _ENV, _stored, True))


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _settings(settings: Settings) -> dict[str, Any]:
    dump = {
        f.name: getattr(settings, f.name)
        for f in dataclasses.fields(settings)
        if f.name != "providers"
    }
    dump = {k: dataclasses.asdict(v) if dataclasses.is_dataclass(v) else v for k, v in dump.items()}
    dump["providers"] = {
        name: dataclasses.asdict(provider) for name, provider in settings.providers.items()
    }
    dump["missing"] = settings.missing
    return json.loads(json.dumps(dump, default=str))


def settings_cases() -> list[dict[str, Any]]:
    """Each case with the settings it loads to and the warnings logged, or the error raised."""
    logger = logging.getLogger("kvasir")
    out = []
    for name, env, stored, strict in SETTINGS_CASES:
        handler = _Warnings()
        logger.addHandler(handler)
        try:
            result: dict[str, Any] = {
                "settings": _settings(Settings.load(dict(env), stored, strict=strict))
            }
        except ConfigError as exc:
            result = {"error": str(exc)}
        finally:
            logger.removeHandler(handler)
        result["warnings"] = handler.lines
        out.append({"name": name, "env": env, "stored": stored, "strict": strict, **result})
    return out


def signatures() -> dict[str, type[dspy.Signature]]:
    found = {}
    for package in PACKAGES:
        path = importlib.import_module(package).__path__
        for module in pkgutil.walk_packages(path, f"{package}."):
            for obj in vars(importlib.import_module(module.name)).values():
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, dspy.Signature)
                    and obj is not dspy.Signature
                ):
                    found[f"{obj.__module__}.{obj.__name__}"] = obj
    return dict(sorted(found.items()))


def sample(annotation: Any, name: str) -> Any:
    """A value of this type that names its field, so a golden shows which value went where."""
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin is list:
        return [sample(get_args(annotation)[0], f"{name} {n}") for n in (1, 2)]
    if origin in (Union, types.UnionType):
        return sample(get_args(annotation)[0], name)
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return annotation(
            **{key: sample(field.annotation, key) for key, field in annotation.model_fields.items()}
        )
    return {str: f"the {name}", int: 3, float: 0.5, bool: True}[annotation]


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    return [_plain(item) for item in value] if isinstance(value, list) else value


def render(signature: type[dspy.Signature], chain_of_thought: bool) -> dict[str, Any]:
    """The prompt, a reply in the adapter's own format, and what that reply parses into."""
    if chain_of_thought:
        signature = dspy.ChainOfThought(signature).predict.signature
    inputs = {k: sample(f.annotation, k) for k, f in signature.input_fields.items()}
    outputs = {k: sample(f.annotation, k) for k, f in signature.output_fields.items()}
    case: dict[str, Any] = {"chain_of_thought": chain_of_thought}
    # The chat format first, then the JSON one a call falls back to when a chat reply fails.
    for prefix, adapter in (("", dspy.ChatAdapter()), ("json_", dspy.JSONAdapter())):
        reply = adapter.format_assistant_message_content(signature, outputs)
        case[f"{prefix}messages"] = adapter.format(signature, [], inputs)
        case[f"{prefix}reply"] = reply
        case[f"{prefix}parsed"] = {k: _plain(v) for k, v in adapter.parse(signature, reply).items()}
    return case


def normalized(urls: list[str]) -> list[dict[str, str | None]]:
    """Each URL with its key, or with `key` null where `normalize` refuses it."""
    corpus = []
    for url in urls:
        try:
            key = normalize(url)
        except ValueError:
            key = None
        corpus.append({"url": url, "key": key})
    return corpus


def _open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def schema(path: Path) -> str:
    with closing(_open(path)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        rows = db.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            " ORDER BY type, name"
        ).fetchall()
    return f"-- user_version {version}\n" + "".join(f"{sql};\n" for (sql,) in rows)


def stored_urls(path: Path) -> list[str]:
    with closing(_open(path)) as db:
        rows = db.execute(
            "SELECT url FROM pages UNION SELECT final_url FROM pages UNION SELECT target FROM links"
        ).fetchall()
    return sorted(url for (url,) in rows if url)


def stored_runs(path: Path) -> list[Any]:
    """The newest run of each kind in each state."""
    with closing(_open(path)) as db:
        rows = db.execute(
            "SELECT detail FROM runs GROUP BY json_extract(summary, '$.kind'), state"
            " HAVING created_at = MAX(created_at) ORDER BY id"
        ).fetchall()
    return [json.loads(detail) for (detail,) in rows]


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    data = parser.parse_args().data

    golden = {
        name: render(signature, signature.__name__ in CHAIN_OF_THOUGHT)
        for name, signature in signatures().items()
    }
    _write(GOLDEN / "signatures.json", golden)
    _write(GOLDEN / "urls.json", normalized(SPELLINGS))
    _write(GOLDEN / "settings.json", settings_cases())
    for name in DATABASES:
        schemas = GOLDEN / "schemas"
        schemas.mkdir(exist_ok=True)
        (schemas / f"{name}.sql").write_text(schema(data / f"{name}.db"))

    private = stored_urls(data / "pages.db")
    _write(data / "golden" / "urls.json", normalized(private))
    runs = stored_runs(data / "kvasir.db")
    _write(data / "golden" / "runs.json", runs)
    print(f"{len(golden)} signatures, {len(private)} stored URLs, {len(runs)} runs")


if __name__ == "__main__":
    main()
