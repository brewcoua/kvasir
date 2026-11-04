"""The page cache: what it holds by site and page, what recall would take from it, and removing or
fetching again what it holds. The routes are sync, so SQLite and the fetch run on a thread."""

from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Request

from kvasir.config import ConfigError, Settings
from kvasir.research import fetch
from kvasir.research.corpus import RECALL_MIN, RECALL_PIECES
from kvasir.research.corroborate import domain
from kvasir.research.fetch import FetchError, normalize
from kvasir.research.pages import PageStore, page_id
from kvasir.runners import build_research_tools
from kvasir.runs.registry import RunRegistry
from kvasir.schemas.pages import (
    CacheStats,
    PageDetail,
    PageLead,
    PageRun,
    PageSummary,
    Recall,
    RecallPiece,
    Site,
    SiteEdge,
    SiteGraph,
    SiteNode,
)
from kvasir.storm.encoder import cosine_similarity

# Pages listed when no site is asked for.
PAGE_LIMIT = 200

router = APIRouter()


class PagesError(Exception):
    """A request the page cache cannot serve, with the status it answers."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _store() -> PageStore:
    store = fetch.cache()
    if store is None:
        raise PagesError(503, "no page cache is configured")
    return store


def _key(store: PageStore, id: str) -> str:
    for row in store.rows():
        if page_id(row.key) == id:
            return row.key
    raise PagesError(404, f"no page {id}")


def _detail(http: Request, store: PageStore, key: str) -> PageDetail:
    found = store.get(key)
    if found is None:
        raise PagesError(404, f"no page {page_id(key)}")
    page = found[0]
    leads: dict[str, list[PageLead]] = {}
    for link in page.links:
        leads.setdefault(link.region, []).append(
            PageLead(url=link.url, anchor=link.anchor, context=link.context)
        )
    registry: RunRegistry = http.app.state.runs
    cited = registry.citing(list(dict.fromkeys([page.final_url, key])))
    return PageDetail(
        id=page_id(key),
        url=key,
        final_url=page.final_url,
        title=page.title,
        engine=page.engine,
        fetched_at=page.fetched_at,
        text=page.text,
        leads=leads,
        runs=[PageRun(id=run.id, topic=run.topic) for run in cited],
    )


@router.get("/v1/sites")
def list_sites() -> list[Site]:
    """Each domain with pages cached, the one fetched from last first."""
    sites: dict[str, Site] = {}
    for row in _store().rows():
        name = domain(row.key)
        site = sites.setdefault(
            name, Site(domain=name, pages=0, pieces=0, fetched_at=row.fetched_at, engines=[])
        )
        site.pages += 1
        site.pieces += row.pieces
        if row.engine not in site.engines:
            site.engines.append(row.engine)
    return list(sites.values())


@router.get("/v1/sites/graph")
def site_graph() -> SiteGraph:
    """The cached sites and, between each two, how many stored links lead from one to the other."""
    store = _store()
    nodes: dict[str, SiteNode] = {}
    for row in store.rows():
        name = domain(row.key)
        nodes.setdefault(name, SiteNode(domain=name, pages=0, links=0)).pages += 1
    counts: dict[tuple[str, str], int] = {}
    for page, target in store.link_pairs():
        source, destination = domain(page), domain(target)
        nodes[source].links += 1
        if destination != source and destination in nodes:
            counts[source, destination] = counts.get((source, destination), 0) + 1
    return SiteGraph(
        sites=list(nodes.values()),
        edges=[SiteEdge(source=s, target=t, count=n) for (s, t), n in counts.items()],
    )


# Before `/v1/pages/{id}`, which would take "stats" for an id.
@router.get("/v1/pages/stats")
def stats() -> CacheStats:
    """What the cache holds and what it takes on disk."""
    return CacheStats(**_store().stats()._asdict())


@router.delete("/v1/pages", status_code=204)
def flush() -> None:
    """Empties the cache, and frees its disk space."""
    _store().flush()


@router.get("/v1/pages")
def list_pages(site: str | None = None) -> list[PageSummary]:
    """The pages of `site`, or the latest `PAGE_LIMIT` of all, newest first."""
    rows = [row for row in _store().rows() if site is None or domain(row.key) == site]
    return [
        PageSummary(
            id=page_id(row.key),
            url=row.key,
            title=row.title,
            engine=row.engine,
            fetched_at=row.fetched_at,
            pieces=row.pieces,
            length=row.length,
        )
        for row in (rows[:PAGE_LIMIT] if site is None else rows)
    ]


# Before `/v1/pages/{id}`, which would take "recall" for an id.
@router.get("/v1/pages/recall")
def recall(http: Request, q: str) -> Recall:
    """The cached pieces recall would take for `q`, scored, without marking any read. For tuning
    `RECALL_MIN`: it lists `RECALL_PIECES` times 4, and those under the minimum are flagged."""
    store = _store()
    settings: Settings = http.app.state.settings
    try:
        encoder, _ = build_research_tools(settings)
        found = store.search(q, encoder.embedding_model_name, [])
        if not found:
            return Recall(minimum=RECALL_MIN, pieces=[])
        scores = cosine_similarity(
            encoder.encode([q], query=True), np.stack([vector for _, _, vector in found])
        )[0]
    except ConfigError as exc:
        raise PagesError(503, str(exc)) from exc
    except Exception as exc:
        raise PagesError(503, f"the embedding model failed: {exc}") from exc
    order = np.argsort(-scores, kind="stable")[: RECALL_PIECES * 4]
    return Recall(
        minimum=RECALL_MIN,
        pieces=[
            RecallPiece(
                page=page_id(found[i][0].url),
                url=found[i][0].url,
                title=found[i][0].title,
                site=domain(found[i][0].url),
                text=found[i][1],
                similarity=float(scores[i]),
                clears=bool(scores[i] >= RECALL_MIN),
            )
            for i in (int(i) for i in order)
        ],
    )


@router.get("/v1/pages/{id}")
def page_detail(http: Request, id: str) -> PageDetail:
    store = _store()
    return _detail(http, store, _key(store, id))


@router.delete("/v1/pages/{id}", status_code=204)
def delete_page(id: str) -> None:
    store = _store()
    store.delete([_key(store, id)])


@router.delete("/v1/sites/{name}", status_code=204)
def delete_site(name: str) -> None:
    store = _store()
    keys = [row.key for row in store.rows() if domain(row.key) == name]
    if not keys:
        raise PagesError(404, f"no pages of {name}")
    store.delete(keys)


@router.post("/v1/pages/{id}/refetch")
def refetch(http: Request, id: str) -> PageDetail:
    """Reads the page again whatever its age. The page it answers with is the one now cached at the
    final URL, which is another when the site has moved the page since it was first read. Its
    pieces go with the old text; the next run to read it makes them."""
    store = _store()
    key = _key(store, id)
    found = store.get(key)
    if found is None:
        raise PagesError(404, f"no page {id}")
    # This request's own context: a sync route runs in a copy of it on a thread.
    fetch.use_max_age(0)
    try:
        page = fetch.fetch_page(found[0].final_url)
    except FetchError as exc:
        raise PagesError(502, str(exc)) from exc
    return _detail(http, store, normalize(page.url))
