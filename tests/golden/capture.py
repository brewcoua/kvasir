"""Writes the golden data the Rust port is compared with. Python stays the reference until cutover.

Run it from the repository root, with a data directory a real service has used:

    uv run python tests/golden/capture.py --data data

Into `tests/golden/`, to commit: `signatures.json`, `urls.json`, and `schemas/`. Into
`<data>/golden/`, which is not committed because it holds what was researched: `urls.json` and
`runs.json` taken from the databases.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import pkgutil
import sqlite3
import types
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import dspy
from pydantic import BaseModel

import kvasir.storm.interface  # noqa: F401  imported first: the fork's modules import circularly
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
    adapter = dspy.ChatAdapter()
    inputs = {k: sample(f.annotation, k) for k, f in signature.input_fields.items()}
    outputs = {k: sample(f.annotation, k) for k, f in signature.output_fields.items()}
    reply = adapter.format_assistant_message_content(signature, outputs)
    return {
        "chain_of_thought": chain_of_thought,
        "messages": adapter.format(signature, [], inputs),
        "reply": reply,
        "parsed": {k: _plain(v) for k, v in adapter.parse(signature, reply).items()},
    }


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
