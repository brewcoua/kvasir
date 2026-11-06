//! The search providers a query goes to, side by side.
//!
//! ```text
//! query -> every enabled provider not cooling down, in parallel
//!       -> one ranked list per provider
//!       -> fused by weighted reciprocal rank, duplicates merged by URL
//! ```
//!
//! The fusion uses each provider's rank, not its score: many return none, and the scores of
//! different engines do not compare. A provider that failed is left alone for as long as its
//! failure says, and the others carry the query.

mod arxiv;
mod brave;
mod crossref;
mod europe_pmc;
mod exa;
mod mojeek;
mod searxng;
mod tavily;
mod wikipedia;

use std::collections::HashSet;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use indexmap::IndexMap;
use kvasir_config::SearchSettings;
use kvasir_fetch::Web;
use kvasir_llm::BoxFuture;
use kvasir_run::RunCtx;
use kvasir_types::runs::SearchResult;
use regex::{Captures, Regex};
use reqwest::StatusCode;
use reqwest::header::RETRY_AFTER;
use serde::Serialize;
use tokio::sync::{Semaphore, SemaphorePermit};
use tokio::time::Instant;

pub use arxiv::Arxiv;
pub use brave::Brave;
pub use crossref::Crossref;
pub use europe_pmc::EuropePmc;
pub use exa::Exa;
pub use mojeek::Mojeek;
pub use searxng::SearXng;
pub use tavily::Tavily;
pub use wikipedia::Wikipedia;

use crate::search::{Hit, RRF_K, Search};

/// How long a provider rests after a rate limit that does not say.
const RATE_LIMIT_COOL_DOWN: Duration = Duration::from_secs(60);
/// How long a provider rests after a CAPTCHA, which nothing can solve yet.
const CAPTCHA_COOL_DOWN: Duration = Duration::from_secs(60 * 60);
const DAY: u64 = 24 * 60 * 60;
/// How long a provider has to answer.
const TIMEOUT: Duration = Duration::from_secs(30);
/// Results asked of a provider that takes a count: the most Brave and Tavily give, and what a
/// search ranks.
const RESULTS: usize = crate::search::POOL;

/// A client for one provider's requests.
fn client() -> reqwest::Client {
    reqwest::Client::builder().timeout(TIMEOUT).build().expect("the TLS backend starts")
}

/// The failure a status means on every provider, if it does.
fn status_failure(response: &reqwest::Response) -> Option<Failure> {
    match response.status() {
        StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => Some(Failure::Unauthorized),
        StatusCode::TOO_MANY_REQUESTS => {
            let retry_after = response
                .headers()
                .get(RETRY_AFTER)
                .and_then(|value| value.to_str().ok())
                .and_then(|text| text.parse().ok())
                .map(Duration::from_secs);
            Some(Failure::RateLimited { retry_after })
        }
        _ => None,
    }
}

/// The same for a provider asked without a key, whose refusal is not about one and may pass.
fn keyless_failure(response: &reqwest::Response) -> Option<Failure> {
    status_failure(response).filter(|failure| *failure != Failure::Unauthorized)
}

/// The text of a snippet of HTML or JATS, on one line.
fn plain_text(markup: &str) -> String {
    // These part words. Another tag may fall inside one, as in `<i>a</i>b`.
    static BREAK: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"</?(?:jats:)?(?:p|br|div|li|sec|title)\b[^>]*>").expect("the pattern is valid")
    });
    static TAG: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"<[^>]*>").expect("the pattern is valid"));
    // One pass, so `&amp;lt;` comes out as the text `&lt;` and not as a bracket.
    static ENTITY: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"&(amp|lt|gt|quot|apos|nbsp|#[0-9]+|#[xX][0-9a-fA-F]+);")
            .expect("the pattern is valid")
    });
    let text = BREAK.replace_all(markup, " ");
    let text = TAG.replace_all(&text, "");
    let text = ENTITY.replace_all(&text, |entity: &Captures<'_>| {
        let name = &entity[1];
        let code = match name.strip_prefix('#') {
            Some(number) => match number.strip_prefix(['x', 'X']) {
                Some(hex) => u32::from_str_radix(hex, 16).ok(),
                None => number.parse().ok(),
            },
            None => Some(match name {
                "amp" => '&',
                "lt" => '<',
                "gt" => '>',
                "quot" => '"',
                "apos" => '\'',
                _ => ' ',
            } as u32),
        };
        code.and_then(char::from_u32).map_or_else(|| entity[0].to_owned(), String::from)
    });
    one_line(&text)
}

fn one_line(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// A failure the provider did not explain, without the request's URL, which may hold its key.
fn other(what: &str, err: reqwest::Error) -> Failure {
    Failure::Other(format!("{what}: {}", err.without_url()))
}

/// One result, in the order its provider ranks it.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Row {
    pub url: String,
    pub title: String,
    pub snippet: String,
}

/// Why a provider gave no results, which says how long it is left alone.
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum Failure {
    #[error("rate limited")]
    RateLimited { retry_after: Option<Duration> },
    #[error("out of quota")]
    QuotaExhausted,
    #[error("answered with a CAPTCHA")]
    Captcha,
    #[error("refused its API key")]
    Unauthorized,
    #[error("{0}")]
    Other(String),
}

/// A search engine, called with one of `web`'s process permits held.
pub trait Provider: Send + Sync {
    /// As shown in the run's searches and in a failure.
    fn name(&self) -> &'static str;

    /// The least time between the starts of two requests. A provider with one is also asked one
    /// request at a time. A query that comes too soon goes without it rather than waiting for it.
    fn interval(&self) -> Duration {
        Duration::ZERO
    }

    /// What readiness may ask without spending anything, if there is such a thing.
    fn probe(&self) -> Option<&str> {
        None
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>>;
}

/// How a provider stands, as `/readyz` shows it.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum State {
    Ok,
    /// `until` is in seconds since the epoch.
    CoolingDown {
        until: f64,
    },
    Unauthorized,
    Unreachable,
}

impl State {
    /// Whether the provider can serve a query, now or once it has rested.
    pub fn usable(self) -> bool {
        matches!(self, Self::Ok | Self::CoolingDown { .. })
    }
}

pub struct Status {
    pub name: &'static str,
    pub state: State,
    pub probe: Option<String>,
}

#[derive(Default)]
struct Gate {
    cooling_until: Option<Instant>,
    /// Stays until the key changes, which rebuilds [`Providers`].
    unauthorized: bool,
    next_call: Option<Instant>,
}

struct Entry {
    provider: Box<dyn Provider>,
    weight: f64,
    gate: Mutex<Gate>,
    in_flight: Semaphore,
}

impl Entry {
    /// Takes the provider's next slot, held as long as the result, or says why this query goes
    /// without it.
    fn enter(&self) -> Result<Option<SemaphorePermit<'_>>, String> {
        let now = Instant::now();
        let mut gate = self.gate.lock().expect("a gate is not held across a panic");
        if gate.unauthorized {
            return Err(Failure::Unauthorized.to_string());
        }
        if gate.cooling_until.is_some_and(|until| now < until) {
            return Err("cooling down".into());
        }
        if gate.next_call.is_some_and(|next| now < next) {
            return Err("waiting on its rate limit".into());
        }
        let interval = self.provider.interval();
        let turn = if interval.is_zero() {
            None
        } else {
            let busy = |_| "waiting on its rate limit".to_owned();
            Some(self.in_flight.try_acquire().map_err(busy)?)
        };
        gate.next_call = Some(now + interval);
        Ok(turn)
    }

    fn state(&self) -> State {
        let now = Instant::now();
        let gate = self.gate.lock().expect("a gate is not held across a panic");
        if gate.unauthorized {
            return State::Unauthorized;
        }
        match gate.cooling_until.filter(|until| now < *until) {
            Some(until) => {
                let at = SystemTime::now() + (until - now);
                let since_epoch = at.duration_since(UNIX_EPOCH).unwrap_or_default();
                State::CoolingDown { until: since_epoch.as_secs_f64() }
            }
            None => State::Ok,
        }
    }

    fn failed(&self, failure: &Failure) {
        let mut gate = self.gate.lock().expect("a gate is not held across a panic");
        let rest = match failure {
            Failure::RateLimited { retry_after } => retry_after.unwrap_or(RATE_LIMIT_COOL_DOWN),
            Failure::QuotaExhausted => until_next_utc_day(SystemTime::now()),
            Failure::Captcha => CAPTCHA_COOL_DOWN,
            Failure::Unauthorized => {
                gate.unauthorized = true;
                return;
            }
            Failure::Other(_) => return,
        };
        gate.cooling_until = Some(Instant::now() + rest);
    }
}

fn until_next_utc_day(now: SystemTime) -> Duration {
    let since_epoch = now.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    Duration::from_secs(DAY - since_epoch % DAY)
}

/// The providers the settings enable. Lives as long as those settings, so a cool down outlasts a
/// run.
pub struct Providers {
    entries: Vec<Entry>,
    web: Arc<Web>,
}

impl Providers {
    /// Each provider with its weight in the fusion.
    pub fn new(providers: Vec<(Box<dyn Provider>, f64)>, web: Arc<Web>) -> Self {
        let entries = providers
            .into_iter()
            .map(|(provider, weight)| Entry {
                provider,
                weight,
                gate: Mutex::default(),
                in_flight: Semaphore::new(1),
            })
            .collect();
        Self { entries, web }
    }

    pub fn from_settings(settings: &SearchSettings, web: Arc<Web>) -> Self {
        let mut providers: Vec<(Box<dyn Provider>, f64)> = Vec::new();
        if settings.searxng.enabled && !settings.searxng.url.is_empty() {
            providers
                .push((Box::new(SearXng::new(&settings.searxng.url)), settings.searxng.weight));
        }
        if let Some(key) = settings.brave.usable_key() {
            providers.push((Box::new(Brave::new(key)), settings.brave.weight));
        }
        if let Some(key) = settings.tavily.usable_key() {
            providers.push((Box::new(Tavily::new(key)), settings.tavily.weight));
        }
        if let Some(key) = settings.exa.usable_key() {
            providers.push((Box::new(Exa::new(key)), settings.exa.weight));
        }
        if let Some(key) = settings.mojeek.usable_key() {
            providers.push((Box::new(Mojeek::new(key)), settings.mojeek.weight));
        }
        if settings.wikipedia.enabled {
            providers.push((Box::new(Wikipedia::new(Arc::clone(&web))), settings.wikipedia.weight));
        }
        if settings.arxiv.enabled {
            providers.push((Box::new(Arxiv::default()), settings.arxiv.weight));
        }
        if settings.crossref.enabled {
            let mailto = settings.crossref.mailto.as_deref();
            providers.push((Box::new(Crossref::new(mailto)), settings.crossref.weight));
        }
        if settings.europe_pmc.enabled {
            providers.push((Box::new(EuropePmc::default()), settings.europe_pmc.weight));
        }
        Self::new(providers, web)
    }

    /// Each provider's state, as far as it is known without asking it.
    pub fn statuses(&self) -> Vec<Status> {
        self.entries
            .iter()
            .map(|entry| Status {
                name: entry.provider.name(),
                state: entry.state(),
                probe: entry.provider.probe().map(str::to_owned),
            })
            .collect()
    }

    /// `entry`'s rows for `query`, reported to `ctx`, or why it has none.
    async fn ask(&self, entry: &Entry, query: &str, ctx: &RunCtx) -> Result<Vec<Row>, String> {
        let name = entry.provider.name();
        let _turn = entry.enter().map_err(|why| format!("{name}: {why}"))?;
        let search = ctx.search_started(name, query);
        let answer = {
            let _permit = self.web.permit().await;
            entry.provider.query(query).await
        };
        match answer {
            Ok(rows) => {
                let reported = rows
                    .iter()
                    .map(|row| SearchResult {
                        title: row.title.clone(),
                        url: row.url.clone(),
                        snippet: row.snippet.clone(),
                        source: None,
                    })
                    .collect();
                ctx.search_ended(search, reported, Vec::new());
                Ok(rows)
            }
            Err(failure) => {
                tracing::error!("{name} failed on query {query}: {failure}");
                entry.failed(&failure);
                let why = format!("{name}: {failure}");
                ctx.search_ended(search, Vec::new(), vec![why.clone()]);
                Err(why)
            }
        }
    }
}

/// The lists as one, best first. Each hit's score is its fused score. A URL in several lists is one
/// hit, with the snippet of each and the score of all.
fn fuse(lists: Vec<(f64, Vec<Row>)>) -> Vec<Hit> {
    let mut hits: IndexMap<String, Hit> = IndexMap::new();
    for (weight, rows) in lists {
        for (rank, row) in rows.into_iter().enumerate() {
            let hit = hits
                .entry(row.url.clone())
                .or_insert_with(|| Hit { url: row.url, ..Hit::default() });
            hit.score += weight / (RRF_K + (rank + 1) as f64);
            if hit.title.is_empty() {
                hit.title = row.title;
            }
            if hit.description.is_empty() {
                hit.description.clone_from(&row.snippet);
            }
            if !row.snippet.is_empty() && !hit.snippets.contains(&row.snippet) {
                hit.snippets.push(row.snippet);
            }
        }
    }
    let mut hits: Vec<Hit> = hits.into_values().collect();
    hits.sort_by(|a, b| b.score.total_cmp(&a.score));
    hits
}

impl Search for Providers {
    fn search<'a>(
        &'a self,
        query: &'a str,
        exclude: &'a HashSet<String>,
        ctx: &'a RunCtx,
    ) -> BoxFuture<'a, (Vec<Hit>, Vec<String>)> {
        Box::pin(async move {
            if self.entries.is_empty() {
                return (Vec::new(), vec!["no search provider is enabled".to_owned()]);
            }
            let answers = futures::future::join_all(
                self.entries.iter().map(|entry| self.ask(entry, query, ctx)),
            )
            .await;
            let mut lists = Vec::new();
            let mut failures = Vec::new();
            for (entry, answer) in self.entries.iter().zip(answers) {
                match answer {
                    Ok(rows) => lists.push((entry.weight, rows)),
                    Err(why) => failures.push(why),
                }
            }
            let hits = fuse(lists).into_iter().filter(|hit| !exclude.contains(&hit.url)).collect();
            (hits, failures)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markup_comes_out_as_text() {
        assert_eq!(
            plain_text(
                "<span class=\"m\">Fo</span>o\n  bar &amp; baz&#39;s &#x3B1;&nbsp;&lt;b&gt;"
            ),
            "Foo bar & baz's \u{3b1} <b>"
        );
        assert_eq!(plain_text("<jats:title>T</jats:title><jats:p>Text</jats:p>"), "T Text");
        assert_eq!(plain_text("&amp;lt; &#xFFFFFFF; &unknown;"), "&lt; &#xFFFFFFF; &unknown;");
    }

    #[test]
    fn a_quota_returns_at_the_next_utc_midnight() {
        let at = |seconds| UNIX_EPOCH + Duration::from_secs(seconds);

        assert_eq!(until_next_utc_day(at(0)), Duration::from_secs(DAY));
        assert_eq!(until_next_utc_day(at(10 * DAY + 3600)), Duration::from_secs(DAY - 3600));
        assert_eq!(until_next_utc_day(at(11 * DAY - 1)), Duration::from_secs(1));
    }
}
