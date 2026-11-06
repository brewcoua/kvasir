//! What kind of site a host is and how far to trust it, the marks a user sets on hosts, and site
//! icons, served from the cache that fills itself on the first request for a site.

use std::collections::BTreeMap;
use std::sync::Arc;

use axum::Json;
use axum::body::Bytes;
use axum::extract::{Path, RawQuery, State};
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get};
use kvasir_sites::sources::{DEADLINE, MarkError, Sources, normalize};
use kvasir_types::sites::{SiteTags, SourceInfo};
use schemars::JsonSchema;
use serde::Deserialize;

use crate::body;
use crate::{ApiError, App};

// An icon is final for a week in the browser. A missing one is asked again after a day, or after a
// minute when the lookup could not tell, so an outage does not hide icons for long.
const ICON_AGE: u32 = 604_800;
const MISS_AGE: u32 = 86_400;
const RETRY_AGE: u32 = 60;

const MAX_TAG_HOSTS: usize = 1000;

#[derive(Deserialize, JsonSchema)]
pub(crate) struct Mark {
    /// `allow`, `deny` or a kind other than `unknown`.
    value: String,
}

#[derive(Deserialize, JsonSchema)]
pub(crate) struct TagsRequest {
    hosts: Vec<String>,
}

pub fn routes() -> axum::Router<Arc<App>> {
    axum::Router::new()
        .route("/v1/sources", delete(flush))
        .route("/v1/sources/tags", get(tags_of_query).post(tags_of_body))
        .route("/v1/sources/{host}", get(source))
        .route("/v1/sources/{host}/mark", delete(unmark).put(mark))
        .route("/v1/favicons", delete(flush_favicons))
        .route("/v1/favicons/{host}", get(favicon))
}

/// `text` as Python's `repr` quotes it, which the messages clients were given use.
fn quoted(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') { '"' } else { '\'' };
    let mut out = String::from(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn sources(app: &App) -> Result<Arc<Sources>, ApiError> {
    app.live()
        .sources
        .clone()
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "sources are off: set sources.enabled"))
}

fn locked(host: &str) -> ApiError {
    let message = format!("{host} is in sources.allow or sources.deny, so a mark cannot change it");
    ApiError::new(StatusCode::CONFLICT, message)
}

async fn site(sources: &Arc<Sources>, host: &str, deadline: std::time::Duration) -> SourceInfo {
    let mut found = sources.classify(&[host.to_owned()], deadline).await;
    found.remove(host).expect("every host asked is answered").info()
}

/// The kind, tags and trust tier of a site, asking Wikidata when nothing else decides it.
async fn source(
    State(app): State<Arc<App>>,
    Path(host): Path<String>,
) -> Result<Json<SourceInfo>, ApiError> {
    if normalize(&host).is_none() {
        let message = format!("{} is not a host name", quoted(&host));
        return Err(ApiError::new(StatusCode::UNPROCESSABLE_ENTITY, message));
    }
    Ok(Json(site(&sources(&app)?, &host, DEADLINE).await))
}

/// What is known of each host now, by the host as sent: nothing waits for Wikidata, whose answer
/// the next request gets. A name that is not a host is `unknown`, so one bad row spares the rest.
async fn tags(app: &App, hosts: &[String]) -> Result<Json<BTreeMap<String, SiteTags>>, ApiError> {
    let sources = sources(app)?;
    if hosts.len() > MAX_TAG_HOSTS {
        let message = format!("at most {MAX_TAG_HOSTS} hosts at a time, got {}", hosts.len());
        return Err(ApiError::new(StatusCode::UNPROCESSABLE_ENTITY, message));
    }
    let found = sources.classify(hosts, std::time::Duration::ZERO).await;
    let tags = found.into_iter().map(|(host, source)| (host, source.info().into()));
    Ok(Json(tags.collect()))
}

/// The tags of the sites named by each `host` in the query.
async fn tags_of_query(
    State(app): State<Arc<App>>,
    RawQuery(query): RawQuery,
) -> Result<Json<BTreeMap<String, SiteTags>>, ApiError> {
    let query = query.unwrap_or_default();
    let hosts: Vec<String> = url::form_urlencoded::parse(query.as_bytes())
        .filter(|(name, _)| name == "host")
        .map(|(_, host)| host.into_owned())
        .collect();
    tags(&app, &hosts).await
}

/// The tags of the sites in the body, for more of them than a URL holds.
async fn tags_of_body(
    State(app): State<Arc<App>>,
    sent: Bytes,
) -> Result<Json<BTreeMap<String, SiteTags>>, ApiError> {
    let value = body::json(&sent)?;
    let at = body::loc(&["body"]);
    body::object(&value, &at)?;
    let TagsRequest { hosts } = body::decode(&value, &at)?;
    tags(&app, &hosts).await
}

/// Forgets the cached Wikidata answers. Marks and the config stay.
async fn flush(State(app): State<Arc<App>>) -> Result<StatusCode, ApiError> {
    sources(&app)?.flush().await?;
    Ok(StatusCode::NO_CONTENT)
}

/// Trusts, blocks or classifies a site, until cleared. A host the config sets answers 409.
async fn mark(
    State(app): State<Arc<App>>,
    Path(host): Path<String>,
    sent: Bytes,
) -> Result<Json<SourceInfo>, ApiError> {
    let value = body::json(&sent)?;
    let at = body::loc(&["body"]);
    body::object(&value, &at)?;
    let Mark { value } = body::decode(&value, &at)?;
    let sources = sources(&app)?;
    if sources.locked(&host) {
        return Err(locked(&host));
    }
    match sources.mark(&host, &value).await {
        Ok(()) => {}
        Err(MarkError::Invalid { host, value }) => {
            let message = format!("cannot mark {} as {}", quoted(&host), quoted(&value));
            return Err(ApiError::new(StatusCode::UNPROCESSABLE_ENTITY, message));
        }
        Err(MarkError::Store(err)) => return Err(err.into()),
    }
    Ok(Json(site(&sources, &host, std::time::Duration::ZERO).await))
}

async fn unmark(
    State(app): State<Arc<App>>,
    Path(host): Path<String>,
) -> Result<StatusCode, ApiError> {
    let sources = sources(&app)?;
    // A mark that outlived a later config entry can still be cleared.
    if sources.locked(&host) && !sources.marked(&host).await? {
        return Err(locked(&host));
    }
    sources.unmark(&host).await?;
    Ok(StatusCode::NO_CONTENT)
}

fn miss(age: u32) -> Response {
    let headers = [
        (header::X_CONTENT_TYPE_OPTIONS, "nosniff".to_owned()),
        (header::CACHE_CONTROL, format!("public, max-age={age}")),
    ];
    (StatusCode::NOT_FOUND, headers).into_response()
}

/// The icon of a site. The type is the one the image's own bytes show, and the response is locked
/// down so a hostile image cannot run anything from this origin.
async fn favicon(State(app): State<Arc<App>>, Path(host): Path<String>) -> Response {
    let Some(favicons) = app.live().favicons.clone() else {
        return miss(MISS_AGE);
    };
    let found = favicons.find(&host).await;
    let Some(icon) = found.icon else {
        return miss(if found.is_final { MISS_AGE } else { RETRY_AGE });
    };
    let headers = [
        (header::CONTENT_TYPE, icon.content_type),
        (header::X_CONTENT_TYPE_OPTIONS, "nosniff".to_owned()),
        (header::CONTENT_SECURITY_POLICY, "default-src 'none'; sandbox".to_owned()),
        (header::CACHE_CONTROL, format!("public, max-age={ICON_AGE}")),
    ];
    (headers, icon.body).into_response()
}

/// Forgets every icon, and frees its disk space.
async fn flush_favicons(State(app): State<Arc<App>>) -> Result<StatusCode, ApiError> {
    if let Some(favicons) = app.live().favicons.clone() {
        favicons.flush().await?;
    }
    Ok(StatusCode::NO_CONTENT)
}

#[cfg(test)]
mod tests {
    use super::quoted;

    #[test]
    fn quotes_as_python_repr() {
        assert_eq!(quoted("not a host"), "'not a host'");
        assert_eq!(quoted("it's"), "\"it's\"");
        assert_eq!(quoted("a'b\"c"), "'a\\'b\"c'");
    }
}
