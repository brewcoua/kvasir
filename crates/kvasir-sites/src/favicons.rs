//! Site icons, fetched on demand and kept in SQLite so a site is asked for its icon once a month.
//!
//! Public icons only. The hosts come from pages a run read, so they are attacker-influenced: every
//! request, redirect hop and `<link>` target is checked, and what comes back is trusted for nothing
//! but its first bytes. The content type served is the one those bytes show, and SVG is refused
//! outright because it would run script when served from this origin.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::Duration;

use dom_query::Document;
use kvasir_config::{FaviconProvider, FaviconSettings};
use kvasir_db::schema::FAVICONS;
use kvasir_db::{Db, DbError, OptionalExtension, params};
use kvasir_extract::too_deep;
use kvasir_fetch::Web;
use kvasir_types::sites::normalize_host;
use regex::Regex;
use reqwest::header;
use tokio::sync::{Semaphore, watch};
use tokio::time::{Instant, timeout, timeout_at};
use url::Url;

use crate::{SECONDS_PER_DAY, now};

// An icon past this is not one, or not worth the disk.
const MAX_BYTES: usize = 256 * 1024;
// Of a homepage, only the head matters, and sites that put it past this are not worth reading on.
const HOMEPAGE_BYTES: usize = 512 * 1024;
const ATTEMPT: Duration = Duration::from_secs(5);
// A whole lookup, across providers. Past it no further provider is tried.
const DEADLINE: Duration = Duration::from_secs(10);
const MAX_REDIRECTS: usize = 3;
const MAX_CANDIDATES: usize = 3;
// Not the politeness of fetches: its interval per host would pace every provider lookup, and a
// research run's permits would starve these.
const WORKERS: usize = 8;
// Past this many lookups in progress, a new one is refused rather than queued without end.
const MAX_FLIGHTS: usize = 64;
const ICON_RELS: [&str; 3] = ["icon", "apple-touch-icon", "apple-touch-icon-precomposed"];

static SIZE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(\d+)x(\d+)").expect("a valid pattern"));

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Icon {
    pub body: Vec<u8>,
    pub content_type: String,
}

/// What a lookup came to. `is_final` is false when it could not tell: the deadline cut it short, or
/// every request failed in transport, so asking again later may find an icon.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Found {
    pub icon: Option<Icon>,
    pub is_final: bool,
}

/// A stored answer: the icon, or None for a site that has none.
#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub icon: Option<Icon>,
    pub provider: Option<String>,
    pub fetched_at: f64,
}

/// One table in one file.
#[derive(Clone, Debug)]
pub struct FaviconStore {
    db: Db,
}

impl FaviconStore {
    pub fn open(path: &Path) -> Result<Self, DbError> {
        Ok(Self { db: Db::open(path, FAVICONS)? })
    }

    /// The answer stored for `host`, unless it is older than `ttl_days` (an icon) or
    /// `miss_ttl_days` (no icon).
    pub async fn get(
        &self,
        host: &str,
        ttl_days: u64,
        miss_ttl_days: u64,
    ) -> Result<Option<Entry>, DbError> {
        let host = host.to_owned();
        let row = self
            .db
            .call(move |db| {
                db.query_row(
                    "SELECT body, content_type, provider, fetched_at FROM favicons WHERE host = ?",
                    [host],
                    |r| {
                        let body: Option<Vec<u8>> = r.get(0)?;
                        let content_type: Option<String> = r.get(1)?;
                        Ok(Entry {
                            icon: body.map(|body| Icon {
                                body,
                                content_type: content_type.unwrap_or_default(),
                            }),
                            provider: r.get(2)?,
                            fetched_at: r.get(3)?,
                        })
                    },
                )
                .optional()
            })
            .await?;
        Ok(row.filter(|entry| {
            let days = if entry.icon.is_some() { ttl_days } else { miss_ttl_days };
            now() - entry.fetched_at < days as f64 * SECONDS_PER_DAY
        }))
    }

    /// Replaces the answer for `host`; no `icon` records that the site has none.
    pub async fn put(
        &self,
        host: &str,
        icon: Option<Icon>,
        provider: Option<&str>,
    ) -> Result<(), DbError> {
        let (host, provider) = (host.to_owned(), provider.map(str::to_owned));
        self.db
            .call(move |db| {
                let (body, content_type) = icon.map(|i| (i.body, i.content_type)).unzip();
                db.execute(
                    "INSERT OR REPLACE INTO favicons VALUES (?, ?, ?, ?, ?)",
                    params![host, body, content_type, provider, now()],
                )
                .map(|_| ())
            })
            .await
    }

    /// Drops every icon, then returns the space to the disk.
    pub async fn flush(&self) -> Result<(), DbError> {
        self.db
            .call(|db| {
                db.execute("DELETE FROM favicons", [])?;
                db.execute_batch("VACUUM; PRAGMA wal_checkpoint(TRUNCATE);")
            })
            .await
    }
}

/// The clock of one lookup, and whether anything answered it. A transport error is no answer.
struct Budget {
    deadline: Instant,
    attempt: Duration,
    heard: bool,
    unresolved: bool,
}

/// Icons by host: from the store while fresh, otherwise from the providers in order.
pub struct Favicons {
    settings: FaviconSettings,
    store: FaviconStore,
    web: Arc<Web>,
    flights: Mutex<HashMap<String, watch::Receiver<Option<Found>>>>,
    workers: Semaphore,
    deadline: Duration,
    attempt: Duration,
    max_flights: usize,
    templates: Templates,
}

/// Where each provider is asked; `{domain}` is the host.
struct Templates {
    direct: String,
    duckduckgo: String,
    google: String,
}

impl Favicons {
    pub fn new(settings: FaviconSettings, store: FaviconStore, web: Arc<Web>) -> Self {
        Self {
            settings,
            store,
            web,
            flights: Mutex::new(HashMap::new()),
            workers: Semaphore::new(WORKERS),
            deadline: DEADLINE,
            attempt: ATTEMPT,
            max_flights: MAX_FLIGHTS,
            templates: Templates {
                direct: "https://{domain}".to_owned(),
                duckduckgo: "https://icons.duckduckgo.com/ip3/{domain}.ico".to_owned(),
                google: "https://www.google.com/s2/favicons?domain={domain}&sz=64".to_owned(),
            },
        }
    }

    /// The icon of `host`, or None when it has none, is not a public site, or favicons are off, and
    /// whether a None is the final answer. Never fails for a failed request or store.
    pub async fn find(self: &Arc<Self>, host: &str) -> Found {
        let unsure = Found { icon: None, is_final: false };
        let key = match normalize_host(host) {
            Some(key) if !self.settings.providers.is_empty() => key,
            _ => return Found { icon: None, is_final: true },
        };
        let mut flight = {
            let mut flights = self.flights.lock().unwrap_or_else(|e| e.into_inner());
            match flights.get(&key) {
                Some(flight) => flight.clone(),
                None if flights.len() >= self.max_flights => return unsure,
                None => {
                    let (done, flight) = watch::channel(None);
                    flights.insert(key.clone(), flight.clone());
                    self.fly(key, done);
                    flight
                }
            }
        };
        // One that outlasts this wait still finishes, and what it finds is stored for the next ask.
        let _ = timeout(self.deadline, flight.wait_for(Option::is_some)).await;
        let found = flight.borrow().clone();
        found.unwrap_or(unsure)
    }

    pub async fn flush(&self) -> Result<(), DbError> {
        self.store.flush().await
    }

    fn fly(self: &Arc<Self>, host: String, done: watch::Sender<Option<Found>>) {
        let this = Arc::clone(self);
        tokio::spawn(async move {
            let work = Arc::clone(&this);
            let name = host.clone();
            let lookup = tokio::spawn(async move {
                let _worker = work.workers.acquire().await.expect("never closed");
                work.resolve(&name).await
            });
            let found = lookup.await;
            if let Err(e) = &found {
                tracing::error!("favicon lookup failed for {host}: {e}");
            }
            // Removed so the table holds only lookups in progress.
            this.flights.lock().unwrap_or_else(|e| e.into_inner()).remove(&host);
            let _ = done.send(found.ok());
        });
    }

    async fn resolve(&self, host: &str) -> Found {
        let settings = &self.settings;
        match self.store.get(host, settings.ttl_days, settings.miss_ttl_days).await {
            Ok(Some(entry)) => return Found { icon: entry.icon, is_final: true },
            Ok(None) => {}
            Err(e) => tracing::warn!("favicon cache unreadable, fetching {host}: {e}"),
        }
        let mut budget = Budget {
            deadline: Instant::now() + self.deadline,
            attempt: self.attempt,
            heard: false,
            unresolved: false,
        };
        let mut icon = None;
        let mut asked = None;
        for provider in &settings.providers {
            if Instant::now() >= budget.deadline {
                break;
            }
            asked = Some(provider);
            icon = self.ask(*provider, host, &mut budget).await;
            if icon.is_some() {
                break;
            }
        }
        // A miss is remembered only when every provider had its turn and one of them answered. One
        // cut short by the deadline might have answered, and slowness or an outage is not an answer.
        if icon.is_none() && (Instant::now() >= budget.deadline || !budget.heard) {
            return Found { icon: None, is_final: false };
        }
        let provider = asked.filter(|_| icon.is_some()).map(|provider| provider_name(*provider));
        if let Err(e) = self.store.put(host, icon.clone(), provider).await {
            tracing::warn!("favicon cache not written for {host}: {e}");
        }
        Found { icon, is_final: true }
    }

    async fn ask(
        &self,
        provider: FaviconProvider,
        host: &str,
        budget: &mut Budget,
    ) -> Option<Icon> {
        match provider {
            FaviconProvider::Direct => self.direct(host, budget).await,
            // `custom` is set by the operator and may be a service on their own network, so its
            // first request skips the check. A redirect from it is still checked.
            FaviconProvider::Custom => {
                let url = self.settings.custom_url.replace("{domain}", host);
                self.icon(&url, budget, true).await
            }
            FaviconProvider::Duckduckgo => {
                let url = self.templates.duckduckgo.replace("{domain}", host);
                self.icon(&url, budget, false).await
            }
            FaviconProvider::Google => {
                let url = self.templates.google.replace("{domain}", host);
                self.icon(&url, budget, false).await
            }
        }
    }

    async fn direct(&self, host: &str, budget: &mut Budget) -> Option<Icon> {
        let origin = self.templates.direct.replace("{domain}", host);
        if let Some(icon) = self.icon(&format!("{origin}/favicon.ico"), budget, false).await {
            return Some(icon);
        }
        // Resolving the name again for the homepage would only wait as long a second time.
        if budget.unresolved {
            return None;
        }
        let home = format!("{origin}/");
        // Its robots.txt may cost a wait of its own, which the deadline is checked against after.
        if !self.web.allowed(&home).await || Instant::now() >= budget.deadline {
            return None;
        }
        let (body, final_url) = self.get(&home, budget, false, HOMEPAGE_BYTES, true).await?;
        let links =
            tokio::task::spawn_blocking(move || icon_links(&body, &final_url)).await.ok()?;
        for url in links.iter().take(MAX_CANDIDATES) {
            if let Some(icon) = self.icon(url, budget, false).await {
                return Some(icon);
            }
        }
        None
    }

    async fn icon(&self, url: &str, budget: &mut Budget, trusted: bool) -> Option<Icon> {
        let (body, _) = self.get(url, budget, trusted, MAX_BYTES, false).await?;
        let content_type = sniff(&body)?;
        Some(Icon { body, content_type: content_type.to_owned() })
    }

    /// The body and final URL of a 2xx answer, or None. Past `limit` bytes it is None, or with
    /// `truncate` the first `limit`. Redirects are followed by hand.
    async fn get(
        &self,
        url: &str,
        budget: &mut Budget,
        trusted: bool,
        limit: usize,
        truncate: bool,
    ) -> Option<(Vec<u8>, String)> {
        let now = Instant::now();
        if now >= budget.deadline {
            return None;
        }
        let give_up = budget.deadline.min(now + budget.attempt);
        let mut url = url.to_owned();
        for hop in 0..=MAX_REDIRECTS {
            let Some(parsed) = Url::parse(&url).ok().filter(|parsed| {
                matches!(parsed.scheme(), "http" | "https") && parsed.host_str().is_some()
            }) else {
                budget.heard = true;
                return None;
            };
            let host = parsed.host_str().unwrap_or("");
            let checked = !(trusted && hop == 0);
            if checked && !self.web.public(host).await {
                // A name that did not resolve may be an outage, which is not an answer.
                budget.unresolved = !self.web.resolves(host).await;
                budget.heard = budget.heard || !budget.unresolved;
                return None;
            }
            let client = if checked { self.web.single() } else { self.web.unguarded() };
            let request = client
                .get(parsed.clone())
                .header(header::USER_AGENT, self.web.user_agent())
                // Compressed, a small read could decode to far more than the limit before it is
                // checked.
                .header(header::ACCEPT_ENCODING, "identity")
                .send();
            let mut response = match timeout_at(give_up, request).await {
                Ok(Ok(response)) => response,
                Ok(Err(e)) => {
                    tracing::warn!("favicon request failed for {url}: {}", e.without_url());
                    return None;
                }
                Err(_) => {
                    tracing::warn!("favicon request timed out for {url}");
                    return None;
                }
            };
            budget.heard = true;
            let status = response.status().as_u16();
            let location = response.headers().get(header::LOCATION);
            if matches!(status, 301 | 302 | 303 | 307 | 308)
                && let Some(location) = location
            {
                let joined = location.to_str().ok().and_then(|l| parsed.join(l).ok())?;
                url = joined.into();
                continue;
            }
            // A placeholder is served with the 404 by some providers, so the body does not count.
            if !response.status().is_success() {
                return None;
            }
            let encoded = response.headers().get(header::CONTENT_ENCODING);
            if encoded.is_some_and(|encoding| encoding != "identity") {
                return None;
            }
            let mut body = Vec::new();
            loop {
                match timeout_at(give_up, response.chunk()).await {
                    Ok(Ok(Some(chunk))) => {
                        body.extend_from_slice(&chunk);
                        if body.len() > limit {
                            if !truncate {
                                return None;
                            }
                            break;
                        }
                    }
                    Ok(Ok(None)) => break,
                    Ok(Err(e)) => {
                        tracing::warn!("favicon read failed for {url}: {}", e.without_url());
                        return None;
                    }
                    // An image cut short would pass the sniff and be kept as if whole.
                    Err(_) if truncate => break,
                    Err(_) => return None,
                }
            }
            body.truncate(limit);
            return Some((body, url));
        }
        None
    }
}

fn provider_name(provider: FaviconProvider) -> &'static str {
    match provider {
        FaviconProvider::Direct => "direct",
        FaviconProvider::Duckduckgo => "duckduckgo",
        FaviconProvider::Google => "google",
        FaviconProvider::Custom => "custom",
    }
}

/// The type of an image format that is safe to serve, from its first bytes alone.
fn sniff(body: &[u8]) -> Option<&'static str> {
    if body.starts_with(b"\x89PNG\r\n\x1a\n") {
        return Some("image/png");
    }
    // An ICO has a count of images, and a CUR has type 2.
    if body.starts_with(b"\x00\x00\x01\x00") && body.len() >= 6 && body[4..6] != [0, 0] {
        return Some("image/x-icon");
    }
    if body.starts_with(b"GIF87a") || body.starts_with(b"GIF89a") {
        return Some("image/gif");
    }
    if body.starts_with(b"\xff\xd8\xff") {
        return Some("image/jpeg");
    }
    if body.starts_with(b"RIFF") && body.get(8..12) == Some(b"WEBP") {
        return Some("image/webp");
    }
    None
}

/// The icons a page declares as absolute URLs, the largest declared first.
fn icon_links(html: &[u8], page_url: &str) -> Vec<String> {
    let html = String::from_utf8_lossy(html);
    let Some(page) = Url::parse(page_url).ok().filter(|_| !too_deep(&html)) else {
        return Vec::new();
    };
    let soup = Document::from(html.as_ref());
    let base = soup.select("base[href]").attr("href");
    let base = base.and_then(|href| page.join(&href).ok()).unwrap_or(page);
    let mut found: Vec<(String, u128)> = Vec::new();
    let mut seen = HashSet::new();
    for link in soup.select("link[href]").iter() {
        let rel = link.attr("rel").unwrap_or_default().to_lowercase();
        if !rel.split_whitespace().any(|token| ICON_RELS.contains(&token)) {
            continue;
        }
        let href = link.attr("href").unwrap_or_default();
        let Ok(url) = base.join(href.trim()) else {
            continue;
        };
        let kind = link.attr("type").unwrap_or_default().trim().to_lowercase();
        if !matches!(url.scheme(), "http" | "https")
            || kind == "image/svg+xml"
            || url.path().to_lowercase().ends_with(".svg")
        {
            continue;
        }
        let sizes = link.attr("sizes").unwrap_or_default().to_lowercase();
        let size = SIZE
            .captures_iter(&sizes)
            .map(|c| {
                let side = |i: usize| c[i].parse::<u128>().unwrap_or(u64::MAX.into());
                side(1).saturating_mul(side(2))
            })
            .max()
            .unwrap_or(0);
        let url = String::from(url);
        if seen.insert(url.clone()) {
            found.push((url, size));
        }
    }
    // Stable, so equally sized icons stay in the page's order.
    found.sort_by_key(|(_, size)| std::cmp::Reverse(*size));
    found.into_iter().map(|(url, _)| url).collect()
}

#[cfg(test)]
#[path = "favicons_tests.rs"]
mod tests;
