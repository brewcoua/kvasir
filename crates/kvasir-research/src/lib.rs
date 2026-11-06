#![doc = include_str!("../README.md")]

pub mod claims;
pub mod clarify;
pub mod corpus;
pub mod corroborate;
pub mod encoder;
pub mod ground;
pub mod providers;
pub mod read;
pub mod render;
pub mod search;
pub mod signatures;
pub mod style;
pub mod text;
pub mod verify;
pub mod write;

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use kvasir_config::{ConfigError, Settings};
use kvasir_fetch::{FetchError, Page, PageStore, Web};
use kvasir_llm::{BoxFuture, Llm, LlmError, RETRY_FOR, Signature, Target, roles};
use kvasir_run::RunCtx;
use kvasir_sites::sources::{Policy, Sources};
use kvasir_types::sites::Tier;

pub use encoder::Encoder;
pub use search::{Hit, Search};

/// What a run's steps call: the models by role, the encoder, the search engine, the web and the
/// source classifier, with the settings they read.
#[derive(Clone)]
pub struct Tools {
    pub llm: Arc<dyn Llm>,
    /// By role: those of `kvasir_llm::roles::research`, and `ground` and `ground_intake` when the
    /// grounding checker is to check sentences and claims.
    pub targets: HashMap<&'static str, Target>,
    pub encoder: Encoder,
    pub search: Arc<dyn Search>,
    pub pages: Arc<dyn Pages>,
    pub sources: Option<Arc<dyn Classify>>,
    pub context_tokens: usize,
    /// How many calls one step makes side by side.
    pub max_threads: usize,
    /// Overrides `fetch.max_age_hours` for this run.
    pub max_age: Option<u32>,
}

impl Tools {
    pub fn target(&self, role: &str) -> &Target {
        self.targets.get(role).unwrap_or_else(|| panic!("no model for {role}"))
    }

    /// Calls the model of `role` with signature `S`.
    pub async fn predict<S: Signature>(
        &self,
        ctx: &RunCtx,
        role: &str,
        input: &S::Input,
    ) -> Result<S::Output, LlmError> {
        kvasir_llm::predict::<S>(&*self.llm, self.target(role), input, RETRY_FOR, ctx).await
    }
}

/// `result`, with a reply that did not parse as None: a step that cannot read one answer carries
/// on without it, while a model that cannot be reached ends the run.
pub fn readable<T>(result: Result<T, LlmError>) -> Result<Option<T>, LlmError> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(LlmError::Parse { signature, reply }) => {
            tracing::warn!("could not read {signature}: {}", kvasir_extract::head(&reply, 200));
            Ok(None)
        }
        Err(err) => Err(err),
    }
}

// Enough to show which lists are at work, in one line of a run's steps.
const MAX_NOTED: usize = 8;

/// How far each host is trusted: the source classifier, or a stand-in in tests.
pub trait Classify: Send + Sync {
    /// Each of `hosts`, waiting at most `deadline` for what is not known yet.
    fn tiers<'a>(
        &'a self,
        hosts: Vec<String>,
        deadline: Duration,
    ) -> BoxFuture<'a, HashMap<String, Tier>>;

    /// The lists behind what is known of each of `hosts`, by name.
    fn listed_by<'a>(&'a self, _hosts: Vec<String>) -> BoxFuture<'a, HashMap<String, Vec<String>>> {
        Box::pin(async { HashMap::new() })
    }
}

/// Tells the run which sites its results lost, and the lists behind that, since a run's reader
/// otherwise cannot see why a site never appears.
pub async fn note_avoided(sources: &dyn Classify, ctx: &RunCtx, mut hosts: Vec<String>) {
    if hosts.is_empty() {
        return;
    }
    hosts.sort();
    hosts.dedup();
    let lists = sources.listed_by(hosts.clone()).await;
    let named: Vec<String> = hosts
        .iter()
        .take(MAX_NOTED)
        .map(|host| match lists.get(host) {
            Some(lists) if !lists.is_empty() => format!("{host} ({})", lists.join(", ")),
            _ => host.clone(),
        })
        .collect();
    let more = match hosts.len().saturating_sub(MAX_NOTED) {
        0 => String::new(),
        more => format!(" and {more} more"),
    };
    ctx.note(format!("left out avoided sites: {}{more}", named.join(", ")));
}

impl Classify for Arc<Sources> {
    fn tiers<'a>(
        &'a self,
        hosts: Vec<String>,
        deadline: Duration,
    ) -> BoxFuture<'a, HashMap<String, Tier>> {
        Box::pin(async move {
            let found = self.classify(&hosts, deadline).await;
            found.into_iter().map(|(host, source)| (host, source.tier())).collect()
        })
    }

    fn listed_by<'a>(&'a self, hosts: Vec<String>) -> BoxFuture<'a, HashMap<String, Vec<String>>> {
        Box::pin(async move {
            let found = self.classify(&hosts, Duration::ZERO).await;
            found.into_iter().map(|(host, source)| (host, source.listed_by)).collect()
        })
    }
}

/// The sources as one run sees them, with the lists it turned on and off for itself.
pub struct Scoped {
    pub sources: Arc<Sources>,
    pub policy: Policy,
}

impl Classify for Scoped {
    fn tiers<'a>(
        &'a self,
        hosts: Vec<String>,
        deadline: Duration,
    ) -> BoxFuture<'a, HashMap<String, Tier>> {
        Box::pin(async move {
            let found = self.sources.classify_with(&hosts, deadline, &self.policy).await;
            found.into_iter().map(|(host, source)| (host, source.tier())).collect()
        })
    }

    fn listed_by<'a>(&'a self, hosts: Vec<String>) -> BoxFuture<'a, HashMap<String, Vec<String>>> {
        Box::pin(async move {
            let found = self.sources.classify_with(&hosts, Duration::ZERO, &self.policy).await;
            found.into_iter().map(|(host, source)| (host, source.listed_by)).collect()
        })
    }
}

/// How pages are read: the web, or a stand-in in tests.
pub trait Pages: Send + Sync {
    fn fetch<'a>(
        &'a self,
        url: &'a str,
        max_age: Option<u32>,
    ) -> BoxFuture<'a, Result<Page, FetchError>>;
    /// Whether a lead may be read, by its host and its site's robots.txt.
    fn allowed<'a>(&'a self, url: &'a str) -> BoxFuture<'a, bool>;
    /// Where pages and their pieces are cached, if anywhere.
    fn cache(&self) -> Option<PageStore>;
}

impl Pages for Web {
    fn fetch<'a>(
        &'a self,
        url: &'a str,
        max_age: Option<u32>,
    ) -> BoxFuture<'a, Result<Page, FetchError>> {
        Box::pin(self.fetch_page(url, max_age))
    }

    fn allowed<'a>(&'a self, url: &'a str) -> BoxFuture<'a, bool> {
        Box::pin(Web::allowed(self, url))
    }

    fn cache(&self) -> Option<PageStore> {
        Web::cache(self)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ResearchError {
    #[error(transparent)]
    Llm(#[from] LlmError),
    /// Searches kept failing, so the run stops rather than write from no evidence.
    #[error("{searches} searches in a row failed: {}", failures.join("; "))]
    SearchUnavailable { searches: usize, failures: Vec<String> },
}

impl ResearchError {
    pub fn unavailable(searches: usize, failures: Vec<String>) -> Self {
        let mut unique = Vec::new();
        for failure in failures {
            if !unique.contains(&failure) {
                unique.push(failure);
            }
        }
        Self::SearchUnavailable { searches, failures: unique }
    }
}

/// One target per research role, keyed by role, with `fast`, `strong` and `reason` in place of
/// the configured models where given. The grounding checker, when one is set, is added as
/// `ground` when `ground` and as `ground_intake` when `ground_intake`.
pub fn targets(
    settings: &Settings,
    fast: Option<&str>,
    strong: Option<&str>,
    reason: Option<&str>,
    ground: bool,
    ground_intake: bool,
) -> Result<HashMap<&'static str, Target>, ConfigError> {
    let given = |name: Option<&str>| name.filter(|n| !n.is_empty()).map(str::to_owned);
    let mut settings = settings.clone();
    if let Some(fast) = given(fast) {
        settings.model.fast = fast;
    }
    if let Some(strong) = given(strong) {
        settings.model.strong = strong;
    }
    if let Some(reason) = given(reason) {
        settings.model.reason = Some(reason);
    }
    let mut targets: HashMap<&'static str, Target> =
        roles::research(&settings)?.into_iter().map(|target| (target.role, target)).collect();
    if let Some(checker) = roles::checker(&settings)? {
        if ground {
            targets.insert("ground", checker.clone());
        }
        if ground_intake {
            targets.insert("ground_intake", checker);
        }
    }
    Ok(targets)
}

/// Today's date in UTC, as `2026-11-05`. A model's sense of "now" is its training cutoff, so a
/// question about what is best or latest is told the date.
pub fn today() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs());
    date(secs)
}

/// The UTC date `secs` after the epoch, by Howard Hinnant's days-to-civil algorithm.
fn date(secs: u64) -> String {
    let z = (secs / 86_400) as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}")
}

#[cfg(test)]
mod tests {
    #[test]
    fn a_date_is_the_utc_day_it_falls_on() {
        assert_eq!(super::date(0), "1970-01-01");
        assert_eq!(super::date(951_782_400), "2000-02-29");
        assert_eq!(super::date(1_793_836_799), "2026-11-04");
        assert_eq!(super::date(4_107_542_400), "2100-03-01");
    }
}
