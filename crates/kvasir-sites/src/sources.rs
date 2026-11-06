//! What kind of site a host is, and how far to trust it: government, news, AI-generated and so on.
//!
//! A host resolves in order: config `allow`/`deny`, a mark, the lists, suffix rules, the cache, then
//! Wikidata. With `rsp`, Wikipedia's perennial sources list adds `rsp_*` tags to what the host
//! resolves to, and sets no kind. With `survey`, a site nothing else decides may be tagged
//! `likely_farm` from its sitemap and popularity. Hosts come from search results, so they are attacker-influenced:
//! they are normalized before they reach a query, and the only text sent to Wikidata is a host or a
//! URL built from one.
//! Nothing here is built unless `[sources] enabled`; the caller decides that.

mod catalog;
mod lists;
mod rsp;
mod store;
mod survey;
mod wikidata;

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use kvasir_config::SourceSettings;
use kvasir_db::DbError;
use kvasir_fetch::Web;
use kvasir_types::sites::{KINDS, SourceInfo, SourceList, Tier, normalize_host, registrable, tier};
use tokio::sync::{Semaphore, watch};
use tokio::time::{Instant, timeout_at};
use url::Url;

pub use catalog::{ListError, catalog};
use lists::{Effect, Listed, Lists};
pub use lists::{Parsed, parse_list};
use rsp::RSP_URL;
pub use rsp::parse_rsp;
pub use store::SourceStore;
use survey::TRANCO_URL;
pub use survey::{Survey, parse_tranco, rate, verdict};

/// Past this a host still unresolved is `unknown` for the pick, and known by the next.
pub const DEADLINE: Duration = Duration::from_secs(5);
const QUERY_TIMEOUT: Duration = Duration::from_secs(30);
const LIST_TIMEOUT: Duration = Duration::from_secs(30);
// The CISA list is about 1 MB; past this a list is not one, or not worth the disk.
const MAX_LIST_BYTES: usize = 16 * 1024 * 1024;
// Lookups wait here so a slow query holds a worker, not the research run.
const WORKERS: usize = 2;
const BUNDLED: &str = include_str!("sources.toml");

/// What is known of one host. `tags` are `official`, `community`, `ai_generated`, `content_farm`,
/// `predatory`, `likely_farm`, `rsp_*`, and `allow` or `deny` from the config or a mark.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Source {
    /// The host as asked, lowercase and without `www.`.
    pub host: String,
    pub kind: String,
    pub tags: BTreeSet<String>,
    pub label: String,
    pub qid: String,
    /// config, mark, list, rule, wikidata or survey; empty when nothing is known.
    pub origin: String,
    /// Why the survey tagged it; empty otherwise.
    pub evidence: String,
    /// The lists that decided or tagged it, by name.
    pub listed_by: Vec<String>,
}

impl Source {
    fn new(host: &str, kind: &str, tags: &[&str], origin: &str) -> Self {
        Self {
            host: host.to_owned(),
            kind: kind.to_owned(),
            tags: tags.iter().map(|tag| (*tag).to_owned()).collect(),
            label: String::new(),
            qid: String::new(),
            origin: origin.to_owned(),
            evidence: String::new(),
            listed_by: Vec::new(),
        }
    }

    pub fn tier(&self) -> Tier {
        tier(&self.kind, self.tags.iter().map(String::as_str))
    }

    pub fn info(&self) -> SourceInfo {
        SourceInfo {
            kind: self.kind.clone(),
            tags: self.tags.iter().cloned().collect(),
            tier: self.tier(),
            label: self.label.clone(),
            qid: self.qid.clone(),
            origin: self.origin.clone(),
            evidence: self.evidence.clone(),
        }
    }
}

/// What Wikidata said of a host.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Match {
    pub kind: String,
    pub tags: BTreeSet<String>,
    pub label: String,
    pub qid: String,
}

/// A mark could not be set.
#[derive(Debug, thiserror::Error)]
pub enum MarkError {
    #[error("cannot mark {host:?} as {value:?}")]
    Invalid { host: String, value: String },
    #[error(transparent)]
    Store(#[from] DbError),
}

/// Sources by host: from the config, marks, lists and rules at once, otherwise from the cache or
/// Wikidata.
pub struct Sources {
    settings: SourceSettings,
    store: SourceStore,
    data_dir: PathBuf,
    web: Arc<Web>,
    query_client: reqwest::Client,
    list_client: reqwest::Client,
    allow: HashSet<String>,
    deny: HashSet<String>,
    // Held across the read of the cache: a flight must not end between that read and this table's.
    flights: tokio::sync::Mutex<HashMap<String, watch::Receiver<()>>>,
    workers: Semaphore,
    backoff_until: Mutex<Option<Instant>>,
    bundled: HashMap<String, Source>,
    lists: Mutex<Option<Arc<Lists>>>,
    specs: Mutex<Arc<Vec<SourceList>>>,
    first_load: tokio::sync::Mutex<()>,
    lists_checked: Mutex<Option<Instant>>,
    rsp_url: String,
    tranco_url: String,
    max_list_bytes: usize,
    // The sites the survey tagged, with why, by registrable domain.
    flags: Mutex<Arc<HashMap<String, String>>>,
    surveying: AtomicBool,
    // Where a surveyed site is asked instead of its own host: a test's fake site.
    survey_base: Option<String>,
}

impl Sources {
    pub fn new(
        settings: SourceSettings,
        store: SourceStore,
        data_dir: PathBuf,
        web: Arc<Web>,
    ) -> Self {
        let allow = hosts(&settings.allow);
        let deny = hosts(&settings.deny);
        // The endpoint and the lists are the operator's, so they are not held to the public check.
        // The query is a POST and not followed; a list may have moved.
        let query_client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(QUERY_TIMEOUT)
            .build()
            .expect("the TLS backend starts");
        let list_client = reqwest::Client::builder()
            .timeout(LIST_TIMEOUT)
            .build()
            .expect("the TLS backend starts");
        Self {
            settings,
            store,
            data_dir,
            web,
            query_client,
            list_client,
            allow,
            deny,
            flights: tokio::sync::Mutex::new(HashMap::new()),
            workers: Semaphore::new(WORKERS),
            backoff_until: Mutex::new(None),
            bundled: bundled(),
            lists: Mutex::new(None),
            specs: Mutex::default(),
            first_load: tokio::sync::Mutex::new(()),
            lists_checked: Mutex::new(None),
            rsp_url: RSP_URL.to_owned(),
            tranco_url: TRANCO_URL.to_owned(),
            max_list_bytes: MAX_LIST_BYTES,
            flags: Mutex::default(),
            surveying: AtomicBool::new(false),
            survey_base: None,
        }
    }

    /// Each of `hosts` as a Source. What the config, marks, lists, rules and cache know is returned
    /// at once; the rest wait at most `deadline` for Wikidata, then are `unknown`, and are known by
    /// the next call. Never fails for a failed request or store.
    pub async fn classify(
        self: &Arc<Self>,
        hosts: &[String],
        deadline: Duration,
    ) -> HashMap<String, Source> {
        self.check_lists().await;
        let mut out = HashMap::new();
        let mut asked = Vec::new();
        for raw in hosts {
            match normalize(raw) {
                Some(key) => asked.push((raw, key)),
                None => {
                    out.insert(raw.clone(), Source::new(raw, "unknown", &[], ""));
                }
            }
        }
        let keys: Vec<&String> = asked.iter().map(|(_, key)| key).collect();
        let marks = self.marks_of(&keys).await;
        let mut waiting = Vec::new();
        let mut fetching = Vec::new();
        for (raw, key) in &asked {
            if let Some(fixed) = self.fixed(key, &marks) {
                out.insert((*raw).clone(), self.overlay(fixed, &marks));
                continue;
            }
            let rule = rule(key);
            let entry = self.cached(key).await;
            if entry.is_none() && self.settings.wikidata {
                fetching.push(key.clone());
                if rule.is_none() {
                    waiting.push((*raw, key));
                }
            }
            let source = merge(key, rule, entry.flatten());
            out.insert((*raw).clone(), self.overlay(source, &marks));
        }
        let flights = self.start(fetching).await;
        let end = Instant::now() + deadline;
        for (raw, key) in waiting {
            if let Some(flight) = flights.get(key) {
                let mut flight = flight.clone();
                // A flight's end is its sender going, which `changed` reports as an error.
                let _ = timeout_at(end, flight.changed()).await;
            }
            // Read again even with no flight: a lookup that finished since the first read stored it.
            let entry = self.cached(key).await;
            out.insert(raw.clone(), self.overlay(merge(key, None, entry.flatten()), &marks));
        }
        if self.settings.survey {
            self.note_met(out.values()).await;
        }
        out
    }

    /// Sets `allow`, `deny`, `exempt` or a kind on `host`, until `unmark`.
    pub async fn mark(&self, host: &str, value: &str) -> Result<(), MarkError> {
        let invalid = || MarkError::Invalid { host: host.to_owned(), value: value.to_owned() };
        let known = matches!(value, "allow" | "deny" | "exempt")
            || KINDS[..KINDS.len() - 1].contains(&value);
        let key = normalize(host).filter(|_| known).ok_or_else(invalid)?;
        Ok(self.store.mark(&key, value).await?)
    }

    pub async fn unmark(&self, host: &str) -> Result<(), DbError> {
        match normalize(host) {
            Some(key) => self.store.unmark(&key).await,
            None => Ok(()),
        }
    }

    /// Whether the config's `allow` or `deny` decides `host`, so a mark cannot.
    pub fn locked(&self, host: &str) -> bool {
        normalize(host).is_some_and(|key| {
            chain(&key).iter().any(|name| self.allow.contains(*name) || self.deny.contains(*name))
        })
    }

    pub async fn marked(&self, host: &str) -> Result<bool, DbError> {
        let Some(key) = normalize(host) else {
            return Ok(false);
        };
        Ok(self.store.marks(vec![key.clone()]).await?.contains_key(&key))
    }

    /// Forgets the cached Wikidata answers and surveys; marks and the config stay.
    pub async fn flush(&self) -> Result<(), DbError> {
        self.store.clear().await?;
        self.load_flags().await;
        Ok(())
    }

    /// `source` with what the lists and the survey add. A config entry or a mark decides alone.
    fn overlay(&self, source: Source, marks: &HashMap<String, String>) -> Source {
        self.flag(self.rsp(self.demote(source, marks)))
    }

    /// `source` with what a `demote` list adds, which joins what the rest of the order found.
    fn demote(&self, mut source: Source, marks: &HashMap<String, String>) -> Source {
        if matches!(source.origin.as_str(), "config" | "mark") {
            return source;
        }
        let exempt = is_exempt(&source.host, marks);
        if exempt {
            source.tags.insert("exempt".to_owned());
        }
        if source.origin == "list" {
            return source;
        }
        if let Some(found) = self.listed(&source.host, exempt) {
            source.tags.extend(found.tags);
            source.listed_by = found.listed_by;
            if source.origin.is_empty() {
                source.origin = "list".to_owned();
            }
        }
        source
    }

    /// `source` with the `rsp_*` tags of its host or registrable domain.
    fn rsp(&self, mut source: Source) -> Source {
        if source.origin == "config" || source.origin == "mark" {
            return source;
        }
        let lists = self.current_lists();
        let found =
            lists.rsp.get(&source.host).or_else(|| lists.rsp.get(registrable(&source.host)));
        if let Some(tag) = found {
            source.tags.insert(tag.clone());
            if source.origin.is_empty() {
                source.origin = "list".to_owned();
            }
        }
        source
    }

    async fn marks_of(&self, keys: &[&String]) -> HashMap<String, String> {
        if keys.is_empty() {
            return HashMap::new();
        }
        let names: HashSet<String> =
            keys.iter().flat_map(|key| chain(key)).map(str::to_owned).collect();
        self.store.marks(names.into_iter().collect()).await.unwrap_or_else(|e| {
            tracing::warn!("source marks unreadable: {e}");
            HashMap::new()
        })
    }

    /// The source of `key` when the config, a mark or a list decides it.
    fn fixed(&self, key: &str, marks: &HashMap<String, String>) -> Option<Source> {
        let names = chain(key);
        if names.iter().any(|name| self.deny.contains(*name)) {
            return Some(Source::new(key, "unknown", &["deny"], "config"));
        }
        if names.iter().any(|name| self.allow.contains(*name)) {
            return Some(Source::new(key, "unknown", &["allow"], "config"));
        }
        let exempt = is_exempt(key, marks);
        for name in names.iter().filter(|_| !exempt) {
            if let Some(value) = marks.get(*name) {
                return Some(if value == "allow" || value == "deny" {
                    Source::new(key, "unknown", &[value], "mark")
                } else {
                    Source::new(key, value, &[], "mark")
                });
            }
        }
        // A demotion alone leaves the rest of the order to find out what the site is.
        let joins = |found: &Source| {
            found.kind == "unknown" && found.tags.contains("demoted") && found.tier() == Tier::Low
        };
        self.listed(key, exempt).filter(|found| !joins(found))
    }

    /// What the lists say of `key`: a `trust` list alone, or else the bundled list, or else every
    /// other list that applies. `exempt`, or an `exempt` list, cancels what would lower the site.
    fn listed(&self, key: &str, exempt: bool) -> Option<Source> {
        let names = site_names(key);
        let lists = self.current_lists();
        let hits = lists.hits(&names, rule(key).is_some());
        let exempt = exempt || hits.iter().any(|list| list.effect == Effect::Exempt);
        let mut found = Source::new(key, "unknown", &[], "list");
        let note = |found: &mut Source, list: &Listed| {
            if !list.tag.is_empty() {
                found.tags.insert(list.tag.clone());
            }
            found.listed_by.push(list.name.clone());
        };
        if hits.iter().any(|list| list.effect == Effect::Trust) {
            found.tags.insert("allow".to_owned());
            for list in hits.iter().filter(|list| list.effect == Effect::Trust) {
                note(&mut found, list);
            }
            return Some(found);
        }
        if !exempt && let Some(bundled) = names.iter().find_map(|name| self.bundled.get(*name)) {
            return Some(Source { host: key.to_owned(), ..bundled.clone() });
        }
        let priority = |kind: &str| KINDS.iter().position(|known| *known == kind);
        for list in hits {
            match &list.effect {
                Effect::Exempt | Effect::Trust => continue,
                Effect::Kind(kind) => {
                    if priority(kind) < priority(&found.kind) {
                        found.kind.clone_from(kind);
                    }
                }
                _ if exempt => continue,
                Effect::Block => drop(found.tags.insert("blocked".to_owned())),
                Effect::Demote => drop(found.tags.insert("demoted".to_owned())),
                Effect::Tag(tag) => drop(found.tags.insert(tag.clone())),
            }
            note(&mut found, list);
        }
        (!found.listed_by.is_empty()).then_some(found)
    }

    /// The stored answer of `key`: None when it has none, and with Wikidata off, always.
    async fn cached(&self, key: &str) -> Option<Option<Match>> {
        if !self.settings.wikidata {
            return None;
        }
        let settings = &self.settings;
        self.store.get(key, settings.ttl_days, settings.miss_ttl_days).await.unwrap_or_else(|e| {
            tracing::warn!("source cache unreadable for {key}: {e}");
            None
        })
    }
}

/// Each of `hosts` as a SourceInfo, or nothing when `sources` is None (the feature is off).
pub async fn describe(
    sources: Option<&Arc<Sources>>,
    hosts: &[String],
    deadline: Duration,
) -> HashMap<String, SourceInfo> {
    let Some(sources) = sources else {
        return HashMap::new();
    };
    let found = sources.classify(hosts, deadline).await;
    found.into_iter().map(|(host, source)| (host, source.info())).collect()
}

/// Each of `urls` as the SourceInfo of its site, from what is known at once: nothing waits for
/// Wikidata, and a lookup the cache lacks finishes in the background. Empty when `sources` is None.
pub async fn describe_urls(
    sources: Option<&Arc<Sources>>,
    urls: &[String],
) -> HashMap<String, SourceInfo> {
    let host_of = |url: &String| -> String {
        Url::parse(url).ok().and_then(|url| url.host_str().map(str::to_owned)).unwrap_or_default()
    };
    let hosts: Vec<String> = urls.iter().map(host_of).collect::<HashSet<_>>().into_iter().collect();
    let found = describe(sources, &hosts, Duration::ZERO).await;
    urls.iter()
        .filter_map(|url| found.get(&host_of(url)).map(|info| (url.clone(), info.clone())))
        .collect()
}

/// `normalize_host` of `raw`, which may end in the dot of a fully qualified name.
pub fn normalize(raw: &str) -> Option<String> {
    let raw = raw.trim();
    normalize_host(raw.strip_suffix('.').unwrap_or(raw))
}

fn hosts(names: &[String]) -> HashSet<String> {
    names
        .iter()
        .filter_map(|name| {
            let key = normalize(name);
            if key.is_none() {
                tracing::warn!("source host {name:?} is not a DNS name, ignored");
            }
            key
        })
        .collect()
}

fn bundled() -> HashMap<String, Source> {
    let data: HashMap<String, String> = toml::from_str(BUNDLED).expect("the bundled list is valid");
    data.into_iter()
        .map(|(host, tag)| {
            let source = Source::new("", "unknown", &[&tag], "list");
            (host, source)
        })
        .collect()
}

/// `host` and its parents down to two labels, most specific first.
fn chain(host: &str) -> Vec<&str> {
    let dots: Vec<usize> = host.match_indices('.').map(|(at, _)| at).collect();
    let mut names = vec![host];
    if dots.len() > 1 {
        names.extend(dots[..dots.len() - 1].iter().map(|at| &host[at + 1..]));
    }
    names
}

/// Whether a mark of `exempt` covers `host`. A closer mark of another value does not make it so.
fn is_exempt(host: &str, marks: &HashMap<String, String>) -> bool {
    chain(host).iter().find_map(|name| marks.get(*name)).is_some_and(|value| value == "exempt")
}

/// `host` and its parents down to its registrable domain. A list naming a suffix others register
/// under, such as `ru.com`, speaks for that name and not for every site below it.
fn site_names(host: &str) -> Vec<&str> {
    let domain = registrable(host);
    let mut names = chain(host);
    names.truncate(names.iter().position(|name| *name == domain).map_or(1, |at| at + 1));
    names
}

/// The kind the host's suffix gives, which Wikidata cannot override.
fn rule(host: &str) -> Option<&'static str> {
    let labels: Vec<&str> = host.split('.').collect();
    let last = labels[labels.len() - 1];
    let before = labels.len().checked_sub(2).map(|at| labels[at]);
    if last == "int" || host == "europa.eu" || host.ends_with(".europa.eu") {
        return Some("intergovernmental");
    }
    let country = last.len() == 2 && labels.len() > 1;
    let second = |names: [&str; 2]| country && before.is_some_and(|label| names.contains(&label));
    if matches!(last, "gov" | "mil") || second(["gov", "gouv"]) || host.ends_with(".gc.ca") {
        return Some("government");
    }
    if last == "edu" || second(["ac", "edu"]) {
        return Some("academic");
    }
    None
}

fn merge(host: &str, rule: Option<&str>, found: Option<Match>) -> Source {
    let kind = rule.or(found.as_ref().map(|found| found.kind.as_str())).unwrap_or("unknown");
    let origin = match (rule, &found) {
        (Some(_), _) => "rule",
        (None, Some(_)) => "wikidata",
        (None, None) => "",
    };
    let mut source = Source::new(host, kind, &[], origin);
    if let Some(found) = found {
        source.tags = found.tags;
        source.label = found.label;
        source.qid = found.qid;
    }
    source
}

#[cfg(test)]
mod tests;
