"""Reading STORM's on-disk output back into a response.

`STORMWikiRunner.run()` returns nothing useful. It writes a directory of files and leaves the path
on `runner.article_output_dir`, which is what the service reads rather than recomputing the
topic-to-directory rule. See docs/fork-notes.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from kvasir.models import Citation

POLISHED_ARTICLE = "storm_gen_article_polished.txt"
DRAFT_ARTICLE = "storm_gen_article.txt"
OUTLINE = "storm_gen_outline.txt"
REFERENCES = "url_to_info.json"


class OutputError(Exception):
    """STORM's output directory is missing or unreadable."""


def read_article(directory: Path) -> str:
    """The polished article, or the draft when polishing was disabled or did not produce one."""
    for name in (POLISHED_ARTICLE, DRAFT_ARTICLE):
        text = _read_text(directory / name)
        if text:
            return text
    raise OutputError(f"no article in {directory}: tried {POLISHED_ARTICLE} and {DRAFT_ARTICLE}")


def read_outline(directory: Path) -> str:
    """The outline. Absent when only the research stage ran, which is not an error."""
    return _read_text(directory / OUTLINE)


def read_citations(directory: Path) -> list[Citation]:
    """The sources, numbered as the article's `[n]` markers reference them.

    `url_to_info.json` holds two maps: `url_to_unified_index` assigns each URL its citation number,
    and `url_to_info` holds the source itself. The number comes from the first map; the
    `citation_uuid` field inside a source is a different, per-stage counter and does not match the
    markers in the article.

    Polishing rewrites the article but not this file, so the numbering is the draft's. That holds
    because `remove_duplicate` is left at its default of False, which is what renumbers sources.
    """
    path = directory / REFERENCES
    if not path.is_file():
        return []

    try:
        reference = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OutputError(f"cannot read {path}: {exc}") from exc

    url_to_index = reference.get("url_to_unified_index", {})
    url_to_info = reference.get("url_to_info", {})

    citations = [
        Citation(
            index=index,
            url=url,
            title=str(url_to_info.get(url, {}).get("title", "")),
            snippet=_first_snippet(url_to_info.get(url, {})),
        )
        for url, index in url_to_index.items()
    ]
    citations.sort(key=lambda citation: citation.index)
    return citations


def _first_snippet(info: dict[str, object]) -> str:
    snippets = info.get("snippets")
    if isinstance(snippets, list) and snippets:
        return str(snippets[0])
    return str(info.get("description", ""))


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OutputError(f"cannot read {path}: {exc}") from exc
