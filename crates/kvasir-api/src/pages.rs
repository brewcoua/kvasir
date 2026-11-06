//! The page cache: what it holds by site and page, what recall would take from it, and removing or
//! fetching again what it holds.

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Duration;

use axum::Json;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::routing::{delete, get, post};
use indexmap::IndexMap;
use kvasir_extract::Region;
use kvasir_fetch::{PageStore, domain, page_id};
use kvasir_llm::NoSink;
use kvasir_research::corpus::{RECALL_MIN, RECALL_PIECES};
use kvasir_research::encoder::cosine;
use kvasir_sites::sources::describe;
use kvasir_types::pages::{
    CacheStats, PageDetail, PageLead, PageRun, PageSummary, Recall, RecallPiece, Site, SiteEdge,
    SiteGraph, SiteNode,
};
use serde::Deserialize;
use serde_json::Value;

use crate::{ApiError, App, body, settings};

/// Pages listed when no site is asked for.
const PAGE_LIMIT: usize = 200;
/// Pieces the text search hands recall to score, as the store's search takes by default.
const SEARCH_LIMIT: usize = 200;

pub fn routes() -> axum::Router<Arc<App>> {
    axum::Router::new()
        .route("/v1/sites", get(sites))
        .route("/v1/sites/graph", get(graph))
        .route("/v1/sites/{name}", delete(delete_site))
        .route("/v1/pages", get(list).delete(flush))
        .route("/v1/pages/stats", get(stats))
        .route("/v1/pages/recall", get(recall))
        .route("/v1/pages/{id}", get(detail).delete(delete_page))
        .route("/v1/pages/{id}/refetch", post(refetch))
}

fn store(app: &App) -> Result<PageStore, ApiError> {
    app.web.cache().ok_or_else(|| {
        ApiError::new(StatusCode::SERVICE_UNAVAILABLE, "no page cache is configured")
    })
}

fn no_page(id: &str) -> ApiError {
    ApiError::new(StatusCode::NOT_FOUND, format!("no page {id}"))
}

async fn key(store: &PageStore, id: &str) -> Result<String, ApiError> {
    let rows = store.rows().await?;
    rows.into_iter().map(|row| row.key).find(|key| page_id(key) == id).ok_or_else(|| no_page(id))
}

fn region(region: Region) -> &'static str {
    match region {
        Region::Main => "main",
        Region::Related => "related",
        Region::Nav => "nav",
        Region::Footer => "footer",
    }
}

async fn page_detail(app: &App, store: &PageStore, key: &str) -> Result<PageDetail, ApiError> {
    let Some(cached) = store.get(key).await? else {
        return Err(no_page(&page_id(key)));
    };
    let page = cached.page;
    let mut leads: IndexMap<String, Vec<PageLead>> = IndexMap::new();
    for link in page.links {
        let lead = PageLead { url: link.url, anchor: link.anchor, context: link.context };
        leads.entry(region(link.region).to_owned()).or_default().push(lead);
    }
    let mut urls = vec![page.final_url.clone()];
    if page.final_url != key {
        urls.push(key.to_owned());
    }
    let cited = app.runs.citing(&urls).await?;
    Ok(PageDetail {
        id: page_id(key),
        url: key.to_owned(),
        final_url: page.final_url,
        title: page.title,
        engine: page.engine.name().to_owned(),
        fetched_at: page.fetched_at,
        text: page.text,
        leads,
        runs: cited.into_iter().map(|run| PageRun { id: run.id, topic: run.topic }).collect(),
    })
}

fn count(value: i64) -> u64 {
    u64::try_from(value).unwrap_or(0)
}

/// Each domain with pages cached, the one fetched from last first.
async fn sites(State(app): State<Arc<App>>) -> Result<Json<Vec<Site>>, ApiError> {
    let mut sites: IndexMap<String, Site> = IndexMap::new();
    for row in store(&app)?.rows().await? {
        let name = domain(&row.key);
        let site = sites.entry(name.clone()).or_insert_with(|| Site {
            domain: name,
            pages: 0,
            pieces: 0,
            fetched_at: row.fetched_at,
            engines: Vec::new(),
            source: None,
        });
        site.pages += 1;
        site.pieces += count(row.pieces);
        let engine = row.engine.name();
        if !site.engines.iter().any(|known| known == engine) {
            site.engines.push(engine.to_owned());
        }
    }
    let names: Vec<String> = sites.keys().cloned().collect();
    let mut infos = describe(app.live().sources.as_ref(), &names, Duration::ZERO).await;
    for (name, site) in &mut sites {
        site.source = infos.remove(name);
    }
    Ok(Json(sites.into_values().collect()))
}

/// The cached sites and, between each two, how many stored links lead from one to the other.
async fn graph(State(app): State<Arc<App>>) -> Result<Json<SiteGraph>, ApiError> {
    let store = store(&app)?;
    let mut nodes: IndexMap<String, SiteNode> = IndexMap::new();
    for row in store.rows().await? {
        let name = domain(&row.key);
        let node = nodes.entry(name.clone()).or_insert_with(|| SiteNode {
            domain: name,
            pages: 0,
            links: 0,
            source: None,
        });
        node.pages += 1;
    }
    let mut counts: IndexMap<(String, String), u64> = IndexMap::new();
    for (page, target) in store.link_pairs().await? {
        let (source, destination) = (domain(&page), domain(&target));
        if let Some(node) = nodes.get_mut(&source) {
            node.links += 1;
        }
        if destination != source && nodes.contains_key(&destination) {
            *counts.entry((source, destination)).or_default() += 1;
        }
    }
    let names: Vec<String> = nodes.keys().cloned().collect();
    let mut infos = describe(app.live().sources.as_ref(), &names, Duration::ZERO).await;
    for (name, node) in &mut nodes {
        node.source = infos.remove(name);
    }
    let edges = counts
        .into_iter()
        .map(|((source, target), count)| SiteEdge { source, target, count })
        .collect();
    Ok(Json(SiteGraph { sites: nodes.into_values().collect(), edges }))
}

/// What the cache holds and what it takes on disk.
async fn stats(State(app): State<Arc<App>>) -> Result<Json<CacheStats>, ApiError> {
    let stats = store(&app)?.stats().await?;
    Ok(Json(CacheStats {
        pages: count(stats.pages),
        links: count(stats.links),
        pieces: count(stats.pieces),
        text_bytes: count(stats.text_bytes),
        vector_bytes: count(stats.vector_bytes),
        file_bytes: stats.file_bytes,
        path: stats.path,
        oldest: stats.oldest,
        newest: stats.newest,
    }))
}

/// Empties the cache, and frees its disk space.
async fn flush(State(app): State<Arc<App>>) -> Result<StatusCode, ApiError> {
    store(&app)?.flush().await?;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Deserialize)]
struct ListQuery {
    site: Option<String>,
}

/// The pages of `site`, or the latest `PAGE_LIMIT` of all, newest first.
async fn list(
    State(app): State<Arc<App>>,
    Query(query): Query<ListQuery>,
) -> Result<Json<Vec<PageSummary>>, ApiError> {
    let rows = store(&app)?.rows().await?;
    let rows = rows
        .into_iter()
        .filter(|row| query.site.as_ref().is_none_or(|site| domain(&row.key) == *site));
    let limit = if query.site.is_none() { PAGE_LIMIT } else { usize::MAX };
    let pages = rows
        .take(limit)
        .map(|row| PageSummary {
            id: page_id(&row.key),
            url: row.key,
            title: row.title,
            engine: row.engine.name().to_owned(),
            fetched_at: row.fetched_at,
            pieces: count(row.pieces),
            length: count(row.length),
        })
        .collect();
    Ok(Json(pages))
}

#[derive(Deserialize)]
struct RecallQuery {
    q: Option<String>,
}

/// The cached pieces recall would take for `q`, scored, without marking any read. For tuning
/// `RECALL_MIN`: it lists `RECALL_PIECES` times 4, and those under the minimum are flagged.
async fn recall(
    State(app): State<Arc<App>>,
    Query(query): Query<RecallQuery>,
) -> Result<Json<Recall>, ApiError> {
    // Refused as FastAPI refused a required query parameter that is not there.
    let Some(q) = query.q else {
        return Err(vec![body::missing(body::loc(&["query", "q"]), Value::Null)].into());
    };
    let store = store(&app)?;
    let settings = app.settings();
    let encoder = settings::encoder(&app, &settings)
        .map_err(|err| ApiError::new(StatusCode::SERVICE_UNAVAILABLE, err.to_string()))?;
    let found = store.search(&q, &encoder.name(), &[], SEARCH_LIMIT, &BTreeSet::new()).await?;
    if found.is_empty() {
        return Ok(Json(Recall { minimum: RECALL_MIN, pieces: Vec::new() }));
    }
    let asked = encoder.one(&q, true, &NoSink).await.map_err(|err| {
        ApiError::new(StatusCode::SERVICE_UNAVAILABLE, format!("the embedding model failed: {err}"))
    })?;
    let mut scored: Vec<(f64, _)> =
        found.into_iter().map(|piece| (cosine(&asked, &piece.vector), piece)).collect();
    // Stable, so pieces that score alike keep the search's order.
    scored.sort_by(|a, b| b.0.total_cmp(&a.0));
    let pieces = scored
        .into_iter()
        .take(RECALL_PIECES * 4)
        .map(|(similarity, piece)| RecallPiece {
            page: page_id(&piece.page.url),
            site: domain(&piece.page.url),
            url: piece.page.url,
            title: piece.page.title,
            text: piece.text,
            similarity,
            clears: similarity >= RECALL_MIN,
        })
        .collect();
    Ok(Json(Recall { minimum: RECALL_MIN, pieces }))
}

async fn detail(
    State(app): State<Arc<App>>,
    Path(id): Path<String>,
) -> Result<Json<PageDetail>, ApiError> {
    let store = store(&app)?;
    let key = key(&store, &id).await?;
    Ok(Json(page_detail(&app, &store, &key).await?))
}

async fn delete_page(
    State(app): State<Arc<App>>,
    Path(id): Path<String>,
) -> Result<StatusCode, ApiError> {
    let store = store(&app)?;
    let key = key(&store, &id).await?;
    store.delete(vec![key]).await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn delete_site(
    State(app): State<Arc<App>>,
    Path(name): Path<String>,
) -> Result<StatusCode, ApiError> {
    let store = store(&app)?;
    let rows = store.rows().await?;
    let keys: Vec<String> =
        rows.into_iter().map(|row| row.key).filter(|key| domain(key) == name).collect();
    if keys.is_empty() {
        return Err(ApiError::new(StatusCode::NOT_FOUND, format!("no pages of {name}")));
    }
    store.delete(keys).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// Reads the page again whatever its age. The page it answers with is the one now cached at the
/// final URL, which is another when the site has moved the page since it was first read. Its
/// pieces go with the old text; the next run to read it makes them.
async fn refetch(
    State(app): State<Arc<App>>,
    Path(id): Path<String>,
) -> Result<Json<PageDetail>, ApiError> {
    let store = store(&app)?;
    let key = key(&store, &id).await?;
    let Some(cached) = store.get(&key).await? else {
        return Err(no_page(&id));
    };
    let page = app
        .web
        .fetch_page(&cached.page.final_url, Some(0))
        .await
        .map_err(|err| ApiError::new(StatusCode::BAD_GATEWAY, err.to_string()))?;
    let key = kvasir_extract::normalize(&page.url).unwrap_or(page.url);
    Ok(Json(page_detail(&app, &store, &key).await?))
}
