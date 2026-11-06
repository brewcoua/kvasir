//! The providers named in the environment, and the models each serves, for picking a model.
//!
//! Every provider answers `GET {api_base}/models` with ids. An `/models` entry with
//! `context_length`, as OpenRouter's has, keeps it. A provider that answers `GET /api/version` as
//! Ollama does is also asked `POST /api/show` for each model, which gives its capabilities and the
//! window it was trained with. That window is a hint: the one served, such as Ollama's `num_ctx`, is
//! often far smaller, and `model.context_tokens` must be the served one.

use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::Json;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::get;
use futures::StreamExt;
use kvasir_config::Provider;
use schemars::JsonSchema;
use serde::Serialize;
use serde_json::{Value, json};

use crate::{ApiError, App};

// Long enough that a settings page opening the list again asks nothing, short enough that a model
// pulled since shows up soon.
const CACHE_FOR: Duration = Duration::from_secs(300);
// Ollama answers one show at a time per model; this many keep a long list quick without a flood.
const SHOW_CONCURRENCY: usize = 8;

/// One provider's listing, and when it was made. The lock also keeps a page asking twice at once
/// from asking the provider twice.
#[derive(Default)]
pub(crate) struct Cache(tokio::sync::Mutex<Option<(Instant, Vec<ModelInfo>)>>);

#[derive(Debug, Serialize, JsonSchema)]
pub struct ProviderInfo {
    pub name: String,
    /// Whether its model list could be read.
    pub reachable: bool,
}

#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct ModelInfo {
    pub id: String,
    /// What a model setting takes: `<provider>/<id>`.
    #[serde(rename = "ref")]
    pub reference: String,
    /// The window the model was trained with, when the provider says. Not the one it is served
    /// with.
    #[serde(default)]
    pub context_length: Option<u64>,
    /// Such as `completion`, `embedding`, `tools`, `thinking`, when the provider says.
    #[serde(default)]
    pub capabilities: Option<Vec<String>>,
}

pub fn routes() -> axum::Router<Arc<App>> {
    axum::Router::new()
        .route("/v1/providers", get(providers))
        .route("/v1/providers/{name}/models", get(list))
}

/// Every provider, by name. Neither its address nor its key is shown.
async fn providers(State(app): State<Arc<App>>) -> Json<Vec<ProviderInfo>> {
    let settings = app.settings();
    let found = settings.providers.iter().map(|(name, provider)| {
        let app = &app;
        async move {
            let reachable = models(app, name, provider).await.is_ok();
            ProviderInfo { name: name.clone(), reachable }
        }
    });
    Json(futures::future::join_all(found).await)
}

/// The models `name` serves, sorted by id. Cached for a few minutes.
async fn list(
    State(app): State<Arc<App>>,
    Path(name): Path<String>,
) -> Result<Json<Vec<ModelInfo>>, ApiError> {
    let settings = app.settings();
    let Some(provider) = settings.providers.get(&name) else {
        return Err(ApiError::new(StatusCode::NOT_FOUND, format!("no provider {name}")));
    };
    Ok(Json(models(&app, &name, provider).await?))
}

async fn models(app: &App, name: &str, provider: &Provider) -> Result<Vec<ModelInfo>, ApiError> {
    let cache = app.models.get(name).expect("a cache per provider");
    let mut cached = cache.0.lock().await;
    if let Some((at, models)) = cached.as_ref()
        && at.elapsed() < CACHE_FOR
    {
        return Ok(models.clone());
    }
    let listed = listing(app, provider).await.map_err(|err| {
        tracing::warn!("cannot list the models of provider {name}: {err}");
        ApiError::new(StatusCode::BAD_GATEWAY, format!("provider {name} did not list its models"))
    })?;
    let mut models: Vec<ModelInfo> = listed.iter().filter_map(|entry| model(name, entry)).collect();
    let root = provider.api_base.strip_suffix("/v1").unwrap_or(&provider.api_base);
    let mut complete = true;
    if is_ollama(app, root, provider).await {
        let ids: Vec<String> = models.iter().map(|model| model.id.clone()).collect();
        let shown: Vec<Option<Shown>> = futures::stream::iter(ids)
            .map(|id| async move { show(app, root, provider, &id).await })
            .buffered(SHOW_CONCURRENCY)
            .collect()
            .await;
        for (model, shown) in models.iter_mut().zip(shown) {
            let Some(shown) = shown else {
                complete = false;
                continue;
            };
            model.capabilities = shown.capabilities.or(model.capabilities.take());
            model.context_length = shown.context_length.or(model.context_length);
        }
    }
    models.sort_by(|a, b| a.id.cmp(&b.id));
    // A list missing what a model failed to say is served, but asked for again next time.
    if complete {
        *cached = Some((Instant::now(), models.clone()));
    }
    Ok(models)
}

async fn listing(app: &App, provider: &Provider) -> Result<Vec<Value>, String> {
    let response = app
        .probe
        .get(format!("{}/models", provider.api_base))
        .bearer_auth(&provider.api_key)
        .send()
        .await
        .and_then(reqwest::Response::error_for_status)
        .map_err(|err| err.to_string())?;
    let body: Value = response.json().await.map_err(|err| err.to_string())?;
    match body.get("data") {
        Some(Value::Array(entries)) => Ok(entries.clone()),
        _ => Err("no data list in the answer".into()),
    }
}

/// A positive integer, which is all a length can be.
fn length(value: Option<&Value>) -> Option<u64> {
    value.and_then(Value::as_u64).filter(|length| *length > 0)
}

/// One `/models` entry, or None without a usable id. A field of an odd shape is dropped.
fn model(name: &str, entry: &Value) -> Option<ModelInfo> {
    let id = entry.get("id")?.as_str()?;
    Some(ModelInfo {
        id: id.to_owned(),
        reference: format!("{name}/{id}"),
        context_length: length(entry.get("context_length")),
        capabilities: None,
    })
}

/// Whether `root` answers as Ollama does, asked once rather than once for each model.
async fn is_ollama(app: &App, root: &str, provider: &Provider) -> bool {
    let request = app.probe.get(format!("{root}/api/version")).bearer_auth(&provider.api_key);
    let Ok(response) = request.send().await else { return false };
    if !response.status().is_success() {
        return false;
    }
    let body: Option<Value> = response.json().await.ok();
    body.is_some_and(|body| body.get("version").is_some_and(Value::is_string))
}

struct Shown {
    capabilities: Option<Vec<String>>,
    context_length: Option<u64>,
}

/// What Ollama says of `model`, or None when it could not be asked.
async fn show(app: &App, root: &str, provider: &Provider, model: &str) -> Option<Shown> {
    let asked = async {
        let response = app
            .probe
            .post(format!("{root}/api/show"))
            .bearer_auth(&provider.api_key)
            .json(&json!({ "model": model }))
            .send()
            .await
            .and_then(reqwest::Response::error_for_status)
            .map_err(|err| err.to_string())?;
        match response.json::<Value>().await.map_err(|err| err.to_string())? {
            shown @ Value::Object(_) => Ok(shown),
            _ => Err("not an object".to_owned()),
        }
    };
    let shown = match asked.await {
        Ok(shown) => shown,
        Err(err) => {
            tracing::warn!("cannot ask Ollama at {root} about {model}: {err}");
            return None;
        }
    };
    let capabilities = shown.get("capabilities").and_then(Value::as_array).and_then(|list| {
        list.iter().map(|c| c.as_str().map(str::to_owned)).collect::<Option<Vec<_>>>()
    });
    let info = shown.get("model_info");
    let architecture =
        info.and_then(|info| info.get("general.architecture")).and_then(Value::as_str);
    let context_length = architecture
        .and_then(|arch| length(info.and_then(|info| info.get(format!("{arch}.context_length")))));
    Some(Shown { capabilities, context_length })
}
