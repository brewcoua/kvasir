#![doc = include_str!("../README.md")]

mod auth;
mod body;
mod methods;
mod openapi;
mod page;
mod pages;
mod providers;
mod runs;
mod settings;
mod sources;
mod tasks;

use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::Arc;
use std::time::Duration;

use arc_swap::ArcSwap;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router, middleware};
use kvasir_config::{ConfigError, Settings, SetupRequired};
use kvasir_db::{Db, DbError};
use kvasir_fetch::{Guard, PageStore, Web};
use kvasir_llm::Llm;
use kvasir_run::{NotFound, RunRegistry, RunStore, Slots};
use kvasir_sites::favicons::Favicons;
use kvasir_sites::sources::Sources;
use serde_json::{Value, json};

pub use openapi::document;

const PROBE_TIMEOUT: Duration = Duration::from_secs(5);

/// Which peers' `X-Forwarded-Proto` is believed, as uvicorn's `FORWARDED_ALLOW_IPS` set it: a
/// proxy that ends TLS says so there, and the login cookie is then marked Secure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Proxies {
    Any,
    Only(Vec<IpAddr>),
}

impl Proxies {
    /// `FORWARDED_ALLOW_IPS`: `*`, or addresses separated by commas. Unset, only this machine.
    pub fn parse(value: Option<&str>) -> Self {
        let value = value.unwrap_or("127.0.0.1");
        if value.split(',').any(|part| part.trim() == "*") {
            return Self::Any;
        }
        Self::Only(value.split(',').filter_map(|part| part.trim().parse().ok()).collect())
    }

    fn trusts(&self, peer: IpAddr) -> bool {
        match self {
            Self::Any => true,
            Self::Only(addresses) => addresses.contains(&peer),
        }
    }
}

/// What a settings change replaces whole, so a request sees one consistent set.
pub(crate) struct Live {
    pub settings: Arc<Settings>,
    pub favicons: Option<Arc<Favicons>>,
    pub sources: Option<Arc<Sources>>,
}

/// The state every route shares.
pub struct App {
    /// As read at startup, to tell which changed settings wait for a restart.
    pub(crate) started: Settings,
    pub(crate) live: ArcSwap<Live>,
    /// One settings change at a time, so two cannot each pass against the rows the other replaces.
    pub(crate) changing: tokio::sync::Mutex<()>,
    pub(crate) db: Db,
    pub(crate) runs: RunRegistry,
    pub(crate) slots: Slots,
    pub(crate) web: Arc<Web>,
    pub(crate) llm: Arc<dyn Llm>,
    /// For readiness and model listings, which wait no longer than a client would.
    pub(crate) probe: reqwest::Client,
    pub(crate) models: HashMap<String, providers::Cache>,
    pub(crate) proxies: Proxies,
}

impl App {
    pub(crate) fn live(&self) -> Arc<Live> {
        self.live.load_full()
    }

    pub(crate) fn settings(&self) -> Arc<Settings> {
        Arc::clone(&self.live.load().settings)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum StartError {
    #[error(transparent)]
    Db(#[from] DbError),
    #[error(transparent)]
    Config(#[from] ConfigError),
    #[error("the HTTP client cannot start: {0}")]
    Http(#[from] reqwest::Error),
}

/// The service for `env`, the environment's settings, with those stored in `kvasir.db` applied.
/// `guard` is what the web may reach, which only a test widens.
pub async fn open(
    env: Settings,
    guard: Guard,
    proxies: Proxies,
    llm: Option<Arc<dyn Llm>>,
) -> Result<Arc<App>, StartError> {
    // Created at the first start, whatever runs.store is.
    let db = Db::open(&env.db(), &env.secret_key, kvasir_db::schema::KVASIR)?;
    let mut warnings = Vec::new();
    let rows = kvasir_config::store::load(&db).await?;
    let stored = kvasir_config::decode(rows, false, &mut warnings)?;
    let settings = env.with_stored(&stored, false, &mut warnings)?;
    for warning in &warnings {
        tracing::warn!("{warning}");
    }

    let web = Arc::new(Web::new(
        settings::fetch_options(&settings),
        settings.max_threads,
        guard,
        kvasir_fetch::system_lookup(),
    ));
    web.set_cache(Some(PageStore::open(&settings.pages_db(), &settings.secret_key)?));
    let live = settings::build(&settings, None, &web)?;
    settings::configure(&web, &settings, None);
    let store = match settings.runs.store {
        kvasir_config::RunStore::Sqlite => Some(RunStore::open(
            &settings.db(),
            &settings.secret_key,
            settings.runs.ttl_days,
            settings.runs.max,
        )?),
        kvasir_config::RunStore::Memory => None,
    };
    // Once, here. Expiry needs no scheduler and no background task.
    if let Some(store) = &store {
        store.sweep().await?;
    }
    let llm = match llm {
        Some(llm) => llm,
        None => Arc::new(kvasir_llm::gateway::Gateway::new()?),
    };
    let providers: Vec<String> = settings
        .providers
        .iter()
        .map(|(name, provider)| format!("{name}={}", provider.api_base))
        .collect();
    tracing::info!(
        "kvasir ready, providers {}, searxng {}, {} concurrent run(s)",
        providers.join(", "),
        settings.search.url,
        settings.runs.max_concurrent
    );
    let missing = settings.missing();
    if !missing.is_empty() {
        tracing::warn!("nothing can run until these are set: {}", missing.join(", "));
    }
    Ok(Arc::new(App {
        started: settings.clone(),
        models: settings.providers.keys().map(|name| (name.clone(), Default::default())).collect(),
        runs: RunRegistry::new(settings.runs.max, store, settings.runs.keep_call_content),
        slots: Slots::new(settings.runs.max_concurrent),
        live: ArcSwap::from_pointee(live),
        changing: tokio::sync::Mutex::new(()),
        db,
        web,
        llm,
        probe: reqwest::Client::builder().timeout(PROBE_TIMEOUT).build()?,
        proxies,
    }))
}

pub fn router(app: Arc<App>) -> Router {
    let guarded = Router::new()
        .merge(runs::routes())
        .merge(methods::routes())
        .merge(pages::routes())
        .merge(sources::routes())
        .merge(tasks::routes())
        .merge(settings::routes())
        .merge(providers::routes())
        .route_layer(middleware::from_fn_with_state(Arc::clone(&app), auth::require_token));
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/", get(page::index))
        .route("/assets/{*path}", get(page::asset))
        .route("/v1/login", post(auth::login))
        .merge(guarded)
        .fallback(|| async { detail(StatusCode::NOT_FOUND, "Not Found") })
        .method_not_allowed_fallback(|| async {
            detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed")
        })
        .layer(middleware::from_fn(access))
        .with_state(app)
}

/// One line per request, once its response starts, as uvicorn's access log wrote. The query is
/// left out: a recall's carries the question.
async fn access(request: axum::extract::Request, next: middleware::Next) -> Response {
    let method = request.method().clone();
    let path = request.uri().path().to_owned();
    let response = next.run(request).await;
    tracing::info!(target: "kvasir::access", "{method} {path} {}", response.status().as_u16());
    response
}

/// Starlette's own error body.
fn detail(status: StatusCode, text: &str) -> Response {
    (status, Json(json!({ "detail": text }))).into_response()
}

/// Liveness. Deliberately checks no dependency: a provider outage must not restart the pod.
async fn healthz() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

/// Readiness. Fails while a required setting is unset, or when SearXNG or any provider a model
/// uses is unreachable. No check spends tokens: listing models is free, and SearXNG is only asked
/// for its root. A provider no model refers to is not probed, since no run needs it.
async fn readyz(State(app): State<Arc<App>>) -> Response {
    let settings = app.settings();
    let missing = settings.missing();
    if !missing.is_empty() {
        let body = json!({ "setup": "required", "missing": missing });
        return (StatusCode::SERVICE_UNAVAILABLE, Json(body)).into_response();
    }
    let model = &settings.model;
    let refs =
        [Some(&model.fast), Some(&model.strong), model.checker.as_ref(), Some(&model.embedding)];
    let mut names: Vec<&str> = refs
        .into_iter()
        .flatten()
        .filter(|reference| !reference.is_empty())
        .map(|reference| reference.split_once('/').map_or(reference.as_str(), |(name, _)| name))
        .collect();
    names.sort_unstable();
    names.dedup();
    let probes = names.iter().map(|name| async {
        let Some(provider) = settings.providers.get(*name) else { return false };
        let request =
            app.probe.get(format!("{}/models", provider.api_base)).bearer_auth(&provider.api_key);
        reachable(request).await
    });
    let (probed, searxng) = tokio::join!(
        futures::future::join_all(probes),
        reachable(app.probe.get(&settings.search.url))
    );
    let ready = searxng && probed.iter().all(|ok| *ok);
    let providers: serde_json::Map<String, Value> =
        names.iter().zip(probed).map(|(name, ok)| ((*name).to_owned(), ok.into())).collect();
    let status = if ready { StatusCode::OK } else { StatusCode::SERVICE_UNAVAILABLE };
    (status, Json(json!({ "providers": providers, "searxng": searxng }))).into_response()
}

async fn reachable(request: reqwest::RequestBuilder) -> bool {
    match request.send().await {
        Ok(response) => response.status().as_u16() < 500,
        Err(err) => {
            tracing::warn!("readiness check failed: {err}");
            false
        }
    }
}

/// Why a route refused, as the FastAPI service answered it.
#[derive(Debug)]
pub(crate) enum ApiError {
    /// `{"message": ...}` with this status.
    Message(StatusCode, String),
    /// 422 with what pydantic would have found.
    Invalid(Vec<body::Detail>),
    Unauthorized,
}

impl ApiError {
    pub fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self::Message(status, message.into())
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        match self {
            Self::Message(status, message) => {
                (status, Json(json!({ "message": message }))).into_response()
            }
            Self::Invalid(detail) => {
                (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({ "detail": detail })))
                    .into_response()
            }
            Self::Unauthorized => (
                StatusCode::UNAUTHORIZED,
                [("www-authenticate", "Bearer")],
                Json(json!({ "message": "missing or wrong token" })),
            )
                .into_response(),
        }
    }
}

impl From<Vec<body::Detail>> for ApiError {
    fn from(detail: Vec<body::Detail>) -> Self {
        Self::Invalid(detail)
    }
}

impl From<SetupRequired> for ApiError {
    fn from(err: SetupRequired) -> Self {
        Self::new(StatusCode::CONFLICT, format!("set these settings first: {err}"))
    }
}

impl From<NotFound> for ApiError {
    fn from(err: NotFound) -> Self {
        match err {
            NotFound::Run(id) => Self::new(StatusCode::NOT_FOUND, format!("no run {id}")),
            NotFound::Call(call) => {
                Self::new(StatusCode::NOT_FOUND, format!("no content kept for call {call}"))
            }
            NotFound::Search(search) => {
                Self::new(StatusCode::NOT_FOUND, format!("no results kept for search {search}"))
            }
            NotFound::Store(err) => err.into(),
        }
    }
}

impl From<DbError> for ApiError {
    fn from(err: DbError) -> Self {
        tracing::error!("a store failed: {err}");
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
    }
}
