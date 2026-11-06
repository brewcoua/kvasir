//! The stored settings: what each is, its value, and changing it.
//!
//! A change is checked against every other stored setting before anything is written, then applies
//! to the running service at once, except the few read only at startup, which say so with
//! `restart`. What a change rebuilds is built before anything is written, so a failure leaves the
//! running settings whole.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;

use axum::Json;
use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::{delete, get};
use kvasir_config::{FIELDS, Field, Kind, Render, Settings};
use kvasir_db::DbError;
use kvasir_fetch::Web;
use kvasir_llm::Target;
use kvasir_research::providers::Providers;
use kvasir_research::{Classify, Encoder, Pages, Search, Tools};
use kvasir_sites::favicons::{FaviconStore, Favicons};
use kvasir_sites::sources::{SourceStore, Sources};
use schemars::JsonSchema;
use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::{ApiError, App, Live, body};

#[derive(Debug, Serialize, JsonSchema)]
pub struct SettingInfo {
    pub key: String,
    /// `integer`, `number`, `boolean`, `text`, `list` of text, `model`, a `<provider>/<name>`, or
    /// `secret`.
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub help: &'static str,
    /// For a `secret`, only `{"set": <bool>}`.
    pub value: Value,
    pub default: Value,
    /// Whether `value` comes from kvasir.db rather than being the default.
    pub stored: bool,
    /// The service runs nothing while a required setting is empty.
    pub required: bool,
    /// Whether `value` may be null, which `""` stores.
    pub nullable: bool,
    /// Read at startup only, so a change waits for a restart.
    pub restart: bool,
    pub minimum: Option<i64>,
    pub choices: Option<Vec<&'static str>>,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct SettingsView {
    pub fields: Vec<SettingInfo>,
    /// The required settings still empty.
    pub missing: Vec<&'static str>,
    /// Settings changed since startup that apply only at the next.
    pub restart_required: Vec<&'static str>,
}

pub fn routes() -> axum::Router<Arc<App>> {
    axum::Router::new()
        .route("/v1/settings", get(read).patch(patch))
        .route("/v1/settings/{key}", delete(reset))
}

fn info(
    settings: &Settings,
    defaults: &Settings,
    stored: &HashSet<String>,
    field: &Field,
) -> SettingInfo {
    let (kind, minimum, choices) = match field.kind {
        Kind::Int { min } => ("integer", Some(min), None),
        Kind::Float => ("number", Some(0), None),
        Kind::Bool => ("boolean", None, None),
        Kind::Text { choices } => ("text", None, (!choices.is_empty()).then(|| choices.to_vec())),
        Kind::OptionalText => ("text", None, None),
        Kind::Secret => ("secret", None, None),
        Kind::List { choices } => ("list", None, (!choices.is_empty()).then(|| choices.to_vec())),
    };
    let shown = |settings: &Settings| {
        let value = settings.value(field.key).unwrap_or(Value::Null);
        if field.kind == Kind::Secret { json!({"set": !value.is_null()}) } else { value }
    };
    SettingInfo {
        key: field.key.into(),
        kind: if field.is_model() { "model" } else { kind },
        help: field.help,
        value: shown(settings),
        default: shown(defaults),
        stored: stored.contains(field.key),
        required: field.required,
        nullable: field.kind == Kind::OptionalText,
        restart: field.restart,
        minimum,
        choices,
    }
}

async fn view(app: &App) -> Result<SettingsView, ApiError> {
    let settings = app.settings();
    let defaults = app.started.with_stored(&Map::new(), false, &mut Vec::new())?;
    let stored: HashSet<String> =
        kvasir_config::store::load(&app.db).await?.into_iter().map(|(key, _)| key).collect();
    Ok(SettingsView {
        fields: FIELDS.iter().map(|field| info(&settings, &defaults, &stored, field)).collect(),
        missing: settings.missing(),
        restart_required: FIELDS
            .iter()
            .filter(|f| f.restart && settings.value(f.key) != app.started.value(f.key))
            .map(|f| f.key)
            .collect(),
    })
}

impl From<kvasir_config::ConfigError> for ApiError {
    fn from(err: kvasir_config::ConfigError) -> Self {
        Self::new(StatusCode::UNPROCESSABLE_ENTITY, err.to_string())
    }
}

/// Every stored setting, with its value, its default and what it accepts.
async fn read(State(app): State<Arc<App>>) -> Result<Json<SettingsView>, ApiError> {
    Ok(Json(view(&app).await?))
}

/// Sets each key to its value, or back to its default with null. Nothing is written unless every
/// stored setting is valid afterwards; 422 names the first that is not.
async fn patch(State(app): State<Arc<App>>, sent: Bytes) -> Result<Json<SettingsView>, ApiError> {
    let changes = if sent.is_empty() { Value::Null } else { body::json(&sent)? };
    let Value::Object(changes) = changes else {
        return Err(ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "send an object of values by key",
        ));
    };
    change(&app, changes).await
}

/// Sets `key` back to its default. Also removes a key this version does not know.
async fn reset(
    State(app): State<Arc<App>>,
    Path(key): Path<String>,
) -> Result<Json<SettingsView>, ApiError> {
    change(&app, Map::from_iter([(key, Value::Null)])).await
}

async fn change(app: &App, changes: Map<String, Value>) -> Result<Json<SettingsView>, ApiError> {
    let _one = app.changing.lock().await;
    let live = app.live();
    let mut stored = Map::new();
    for (key, text) in kvasir_config::store::load(&app.db).await? {
        if changes.contains_key(&key) {
            continue;
        }
        let Ok(value) = serde_json::from_str(&text) else {
            let message = format!("{key} is not JSON; reset it with null");
            return Err(ApiError::new(StatusCode::UNPROCESSABLE_ENTITY, message));
        };
        stored.insert(key, value);
    }
    stored
        .extend(changes.iter().filter(|(_, v)| !v.is_null()).map(|(k, v)| (k.clone(), v.clone())));
    let changed = live.settings.with_stored(&stored, true, &mut Vec::new())?;
    let next = build(&changed, Some(&live), &app.web)?;
    // As parsed, so the row says what GET shows. A null text field is stored as "", since no row
    // would mean its default instead.
    let rows = changes
        .iter()
        .map(|(key, value)| {
            let parsed = changed.value(key).map(|v| if v.is_null() { Value::from("") } else { v });
            (key.clone(), if value.is_null() { None } else { parsed })
        })
        .collect();
    kvasir_config::store::write(&app.db, rows).await?;
    configure(&app.web, &changed, Some(&live.settings));
    app.slots.set_limit(changed.runs.max_concurrent);
    app.live.store(Arc::new(next));
    Ok(Json(view(app).await?))
}

/// What `settings` need built, reusing what `previous` built for the same settings.
pub fn build(
    settings: &Settings,
    previous: Option<&Live>,
    web: &Arc<Web>,
) -> Result<Live, DbError> {
    let favicons = match previous {
        Some(previous) if previous.settings.favicons == settings.favicons => {
            previous.favicons.clone()
        }
        // The database file is not made when favicons are off.
        _ if settings.favicons.providers.is_empty() => None,
        _ => {
            let store = FaviconStore::open(&settings.favicons_db(), &settings.secret_key)?;
            Some(Arc::new(Favicons::new(settings.favicons.clone(), store, Arc::clone(web))))
        }
    };
    // The runs' own instance, so a mark takes effect for a run in flight.
    let sources = match previous {
        Some(previous) if previous.settings.sources == settings.sources => previous.sources.clone(),
        _ if !settings.sources.enabled => None,
        _ => {
            let store = SourceStore::open(&settings.sources_db(), &settings.secret_key)?;
            let data_dir = settings.data_dir.clone();
            Some(Arc::new(Sources::new(settings.sources.clone(), store, data_dir, Arc::clone(web))))
        }
    };
    // The same instance while its settings stand, so a cool down outlasts a run.
    let search = match previous {
        Some(previous) if previous.settings.search == settings.search => {
            Arc::clone(&previous.search)
        }
        _ => Arc::new(Providers::from_settings(&settings.search, Arc::clone(web))),
    };
    Ok(Live { settings: Arc::new(settings.clone()), favicons, sources, search })
}

pub fn fetch_options(settings: &Settings) -> kvasir_fetch::Options {
    let fetch = &settings.fetch;
    kvasir_fetch::Options {
        contact: fetch.contact.clone(),
        host_interval: Duration::from_secs_f64(fetch.host_interval_seconds),
        render: fetch.render == Render::Auto,
        stealth: fetch.stealth,
        max_age_hours: u32::try_from(fetch.max_age_hours).unwrap_or(u32::MAX),
    }
}

/// Points `web` at `settings`, starting a new browser only when how pages render changed.
pub fn configure(web: &Web, settings: &Settings, previous: Option<&Settings>) {
    web.configure(fetch_options(settings));
    let render = |s: &Settings| (s.fetch.render, s.fetch.stealth);
    if previous.is_none_or(|previous| render(previous) != render(settings)) {
        web.set_renderer(renderer(web, settings));
    }
}

#[cfg(feature = "chromium")]
fn renderer(web: &Web, settings: &Settings) -> Option<Arc<dyn kvasir_browser::Render>> {
    (settings.fetch.render == Render::Auto).then(|| {
        let browser = kvasir_browser::Chromium::new(settings.fetch.stealth, web.page_check());
        Arc::new(browser) as Arc<dyn kvasir_browser::Render>
    })
}

#[cfg(not(feature = "chromium"))]
fn renderer(_: &Web, _: &Settings) -> Option<Arc<dyn kvasir_browser::Render>> {
    None
}

/// What a run calls: the models `targets` name, and the services `live` holds. `max_age` overrides
/// `fetch.max_age_hours` for this run.
pub fn tools(
    app: &App,
    live: &Live,
    targets: HashMap<&'static str, Target>,
    max_age: Option<u64>,
) -> Result<Tools, kvasir_config::ConfigError> {
    let settings = &live.settings;
    Ok(Tools {
        llm: Arc::clone(&app.llm),
        targets,
        encoder: encoder(app, settings)?,
        search: Arc::clone(&live.search) as Arc<dyn Search>,
        pages: Arc::clone(&app.web) as Arc<dyn Pages>,
        sources: live.sources.clone().map(|sources| Arc::new(sources) as Arc<dyn Classify>),
        context_tokens: settings.model.context_tokens,
        max_threads: settings.max_threads,
        max_age: max_age.map(|hours| u32::try_from(hours).unwrap_or(u32::MAX)),
    })
}

/// The embedding model.
pub fn encoder(app: &App, settings: &Settings) -> Result<Encoder, kvasir_config::ConfigError> {
    Ok(Encoder { llm: Arc::clone(&app.llm), model: settings.resolve(&settings.model.embedding)? })
}
