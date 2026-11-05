//! What kind of site a host is, and how far to trust it: government, news, AI-generated and so on.
//!
//! A host resolves in order: config `allow`/`deny`, a mark, the lists, suffix rules, the cache, then
//! Wikidata. With `rsp`, Wikipedia's perennial sources list adds `rsp_*` tags to what the host
//! resolves to, and sets no kind. Hosts come from search results, so they are attacker-influenced:
//! they are normalized before they reach a query, and the only text sent to Wikidata is a host or a
//! URL built from one.
//! Nothing here is built unless `[sources] enabled`; the caller decides that.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, SystemTime};

use kvasir_config::SourceSettings;
use kvasir_db::rusqlite::params_from_iter;
use kvasir_db::schema::SOURCES;
use kvasir_db::{Db, DbError, OptionalExtension, params};
use kvasir_fetch::Web;
use kvasir_types::sites::{KINDS, LIST_TAGS, SourceInfo, Tier, normalize_host, registrable, tier};
use regex::Regex;
use reqwest::header;
use serde::Deserialize;
use sha1::{Digest, Sha1};
use tokio::sync::{Semaphore, watch};
use tokio::time::{Instant, timeout_at};
use url::Url;

use crate::{SECONDS_PER_DAY, now};

/// Past this a host still unresolved is `unknown` for the pick, and known by the next.
pub const DEADLINE: Duration = Duration::from_secs(5);
const QUERY_TIMEOUT: Duration = Duration::from_secs(30);
const LIST_TIMEOUT: Duration = Duration::from_secs(30);
// The CISA list is about 1 MB; past this a list is not one, or not worth the disk.
const MAX_LIST_BYTES: usize = 16 * 1024 * 1024;
// Every page the perennial sources list transcludes, in one answer: the rows are in its subpages.
const RSP_URL: &str = "https://en.wikipedia.org/w/api.php?action=query&generator=templates\
    &titles=Wikipedia:Reliable_sources/Perennial_sources&gtlnamespace=4&gtllimit=max\
    &prop=revisions&rvprop=content&rvslots=main&formatversion=2&format=json";
// After a transport error or a 5xx, so a down endpoint is not asked again by every search.
const FAILURE_BACKOFF: f64 = 10.0;
const MAX_BACKOFF: f64 = 3600.0;
const BATCH_SIZE: usize = 50;
// WDQS answers a 429 with Retry-After; without one, this long.
const DEFAULT_BACKOFF: f64 = 60.0;
// How often the downloaded lists are checked for age.
const LIST_CHECK: Duration = Duration::from_secs(3600);
// Past this many hosts in flight, a new one is left unknown rather than queued without end.
const MAX_FLIGHTS: usize = 256;
// Lookups wait here so a slow query holds a worker, not the research run.
const WORKERS: usize = 2;
// A row's class on the perennial sources page, and its tag. The least reliable comes last.
const RSP_ORDER: [&str; 4] = ["rsp_reliable", "rsp_mixed", "rsp_unreliable", "rsp_deprecated"];
// Rows whose status does not apply here. Wikipedia is unreliable there for being circular
// (WP:CIRCULAR), an article citing an article; a run citing it is not circular.
const RSP_SKIP: [&str; 1] = ["wikipedia.org"];
const NAMESPACE: &str = "http://www.wikidata.org/entity/";
const ROOT_KINDS: [(&str, &str); 12] = [
    ("Q327333", "government"),
    ("Q2659904", "government"),
    // A country: `canada.ca` is the Government of Canada's site, and its item is the country's.
    ("Q6256", "government"),
    ("Q3918", "academic"),
    ("Q31855", "academic"),
    ("Q4671277", "academic"),
    ("Q1328899", "standards"),
    ("Q11032", "news"),
    ("Q192283", "news"),
    ("Q1153191", "news"),
    ("Q5292", "reference"),
    ("Q43229", "organization"),
];
const INTERNATIONAL: &str = "Q484652";
const NON_GOVERNMENTAL: &str = "Q79913";
const ROOT_TAGS: [(&str, &str); 2] = [("Q171", "community"), ("Q125361038", "ai_generated")];
const BUNDLED: &str = include_str!("sources.toml");

static RSP_ROW: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?m)^\|-").expect("a pattern"));
static RSP_STATUS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"\A\|- class="s-(\w+)""#).expect("a valid pattern"));
static RSP_USES: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\{\{WP:RSPUSES\|([^{}]*)\}\}").expect("a valid pattern"));
static COMMENT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s#").expect("a valid pattern"));

/// What is known of one host. `tags` are `official`, `community`, `ai_generated`, `content_farm`,
/// `predatory`, `rsp_*`, and `allow` or `deny` from the config or a mark.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Source {
    /// The host as asked, lowercase and without `www.`.
    pub host: String,
    pub kind: String,
    pub tags: BTreeSet<String>,
    pub label: String,
    pub qid: String,
    /// config, mark, list, rule or wikidata; empty when nothing is known.
    pub origin: String,
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

/// Two tables in one file: `sources`, a cache that expires, and `marks`, which never do.
#[derive(Clone, Debug)]
pub struct SourceStore {
    db: Db,
}

impl SourceStore {
    pub fn open(path: &Path) -> Result<Self, DbError> {
        Ok(Self { db: Db::open(path, SOURCES)? })
    }

    /// The answer stored for `host`, unless it is older than `ttl_days` (a match) or
    /// `miss_ttl_days` (no match). The inner None is a host Wikidata does not know.
    pub async fn get(
        &self,
        host: &str,
        ttl_days: u64,
        miss_ttl_days: u64,
    ) -> Result<Option<Option<Match>>, DbError> {
        let host = host.to_owned();
        let row = self
            .db
            .call(move |db| {
                db.query_row(
                    "SELECT kind, tags, label, qid, fetched_at FROM sources WHERE host = ?",
                    [host],
                    |r| {
                        let kind: Option<String> = r.get(0)?;
                        let tags: Option<String> = r.get(1)?;
                        let label: Option<String> = r.get(2)?;
                        let qid: Option<String> = r.get(3)?;
                        let fetched_at: f64 = r.get(4)?;
                        let found = kind.map(|kind| Match {
                            kind,
                            tags: tags
                                .unwrap_or_default()
                                .split(',')
                                .filter(|tag| !tag.is_empty())
                                .map(str::to_owned)
                                .collect(),
                            label: label.unwrap_or_default(),
                            qid: qid.unwrap_or_default(),
                        });
                        Ok((found, fetched_at))
                    },
                )
                .optional()
            })
            .await?;
        Ok(row.and_then(|(found, fetched_at)| {
            let days = if found.is_some() { ttl_days } else { miss_ttl_days };
            (now() - fetched_at < days as f64 * SECONDS_PER_DAY).then_some(found)
        }))
    }

    /// Replaces the answer for `host`; no `found` records that Wikidata does not know it.
    pub async fn put(&self, host: &str, found: Option<Match>) -> Result<(), DbError> {
        let host = host.to_owned();
        self.db
            .call(move |db| {
                let (kind, tags, label, qid) = match found {
                    Some(found) => {
                        let tags = found.tags.into_iter().collect::<Vec<_>>().join(",");
                        (Some(found.kind), Some(tags), Some(found.label), Some(found.qid))
                    }
                    None => (None, None, None, None),
                };
                db.execute(
                    "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?)",
                    params![host, kind, tags, label, qid, now()],
                )
                .map(|_| ())
            })
            .await
    }

    /// Forgets every cached answer, and keeps the marks.
    pub async fn clear(&self) -> Result<(), DbError> {
        self.db.call(|db| db.execute("DELETE FROM sources", []).map(|_| ())).await
    }

    pub async fn mark(&self, host: &str, value: &str) -> Result<(), DbError> {
        let (host, value) = (host.to_owned(), value.to_owned());
        self.db
            .call(move |db| {
                db.execute(
                    "INSERT OR REPLACE INTO marks VALUES (?, ?, ?)",
                    params![host, value, now()],
                )
                .map(|_| ())
            })
            .await
    }

    pub async fn unmark(&self, host: &str) -> Result<(), DbError> {
        let host = host.to_owned();
        self.db
            .call(move |db| db.execute("DELETE FROM marks WHERE host = ?", [host]).map(|_| ()))
            .await
    }

    /// The marks set on any of `hosts`, by host.
    pub async fn marks(&self, hosts: Vec<String>) -> Result<HashMap<String, String>, DbError> {
        self.db
            .call(move |db| {
                let marks = vec!["?"; hosts.len()].join(",");
                let sql = format!("SELECT host, value FROM marks WHERE host IN ({marks})");
                let mut found = db.prepare(&sql)?;
                let rows =
                    found.query_map(params_from_iter(hosts), |r| Ok((r.get(0)?, r.get(1)?)))?;
                rows.collect()
            })
            .await
    }
}

/// What the downloaded lists say, as last read from disk.
#[derive(Default)]
struct Lists {
    index: HashMap<String, Source>,
    rsp: HashMap<String, String>,
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
    first_load: tokio::sync::Mutex<()>,
    lists_checked: Mutex<Option<Instant>>,
    rsp_url: String,
    max_list_bytes: usize,
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
            first_load: tokio::sync::Mutex::new(()),
            lists_checked: Mutex::new(None),
            rsp_url: RSP_URL.to_owned(),
            max_list_bytes: MAX_LIST_BYTES,
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
                out.insert((*raw).clone(), self.overlay(fixed));
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
            out.insert((*raw).clone(), self.overlay(source));
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
            out.insert(raw.clone(), self.overlay(merge(key, None, entry.flatten())));
        }
        out
    }

    /// Sets `allow`, `deny` or a kind on `host`, until `unmark`.
    pub async fn mark(&self, host: &str, value: &str) -> Result<(), MarkError> {
        let invalid = || MarkError::Invalid { host: host.to_owned(), value: value.to_owned() };
        let known =
            value == "allow" || value == "deny" || KINDS[..KINDS.len() - 1].contains(&value);
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

    /// Forgets the cached Wikidata answers; marks and the config stay.
    pub async fn flush(&self) -> Result<(), DbError> {
        self.store.clear().await
    }

    /// Downloads each list, and with `rsp` the perennial sources page, that is missing or older than
    /// `ttl_days`, then rebuilds the index. One that fails to download keeps the copy on disk.
    pub async fn refresh_lists(self: &Arc<Self>) {
        *self.lists_checked.lock().unwrap_or_else(|e| e.into_inner()) = Some(Instant::now());
        for entry in self.list_entries() {
            let (label, url) = entry.split_once('=').unwrap_or((&entry, ""));
            let path = self.list_path(url);
            let age = std::fs::metadata(&path)
                .and_then(|meta| meta.modified())
                .ok()
                .and_then(|modified| SystemTime::now().duration_since(modified).ok());
            let ttl = Duration::from_secs(self.settings.ttl_days.saturating_mul(86400));
            if age.is_some_and(|age| age < ttl) {
                continue;
            }
            if let Err(e) = self.download(url, &path).await {
                tracing::warn!("source list {label} not fetched ({url}): {e}");
            }
        }
        self.load_lists().await;
    }

    async fn download(&self, url: &str, path: &Path) -> Result<(), String> {
        let mut response = self
            .list_client
            .get(url)
            .header(header::USER_AGENT, self.web.user_agent())
            .send()
            .await
            .map_err(|e| e.to_string())?;
        if !response.status().is_success() {
            return Err(format!("status {}", response.status().as_u16()));
        }
        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|e| e.to_string())? {
            body.extend_from_slice(&chunk);
            if body.len() > self.max_list_bytes {
                return Err(format!("longer than {} bytes", self.max_list_bytes));
            }
        }
        let (dir, path) = (self.data_dir.clone(), path.to_owned());
        tokio::task::spawn_blocking(move || save(&dir, &path, &body))
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())
    }

    fn list_entries(&self) -> Vec<String> {
        let mut entries = self.settings.lists.clone();
        if self.settings.enabled && self.settings.rsp {
            entries.push(format!("rsp={}", self.rsp_url));
        }
        entries
    }

    async fn check_lists(self: &Arc<Self>) {
        if self.list_entries().is_empty() {
            return;
        }
        // Held across the read, so callers at startup parse the lists once between them.
        let first_load = self.first_load.lock().await;
        let loaded = self.lists.lock().unwrap_or_else(|e| e.into_inner()).is_some();
        if !loaded {
            self.load_lists().await;
        }
        drop(first_load);
        {
            let mut checked = self.lists_checked.lock().unwrap_or_else(|e| e.into_inner());
            if checked.is_some_and(|at| at.elapsed() < LIST_CHECK) {
                return;
            }
            *checked = Some(Instant::now());
        }
        // Its own task: a slow download must not hold up a classification.
        let this = Arc::clone(self);
        tokio::spawn(async move { this.refresh_lists().await });
    }

    fn list_path(&self, url: &str) -> PathBuf {
        let digest: String =
            Sha1::digest(url.as_bytes()).iter().map(|byte| format!("{byte:02x}")).collect();
        self.data_dir.join(format!("sources-{}.txt", &digest[..12]))
    }

    async fn load_lists(self: &Arc<Self>) {
        let this = Arc::clone(self);
        let lists =
            tokio::task::spawn_blocking(move || this.read_lists()).await.unwrap_or_default();
        *self.lists.lock().unwrap_or_else(|e| e.into_inner()) = Some(Arc::new(lists));
    }

    fn read_lists(&self) -> Lists {
        let mut index = HashMap::new();
        for entry in &self.settings.lists {
            let (label, url) = entry.split_once('=').unwrap_or((entry, ""));
            let template = if LIST_TAGS.contains(&label) {
                Source::new("", "unknown", &[label], "list")
            } else if KINDS[..KINDS.len() - 1].contains(&label) {
                Source::new("", label, &[], "list")
            } else {
                tracing::warn!("source list {entry:?} ignored: its label is not a kind or tag");
                continue;
            };
            match std::fs::read_to_string(self.list_path(url)) {
                Ok(text) => {
                    for host in parse_list(&text) {
                        index.insert(host, template.clone());
                    }
                }
                // Not downloaded yet: the first refresh runs after the first load.
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => tracing::warn!("source list {label} unreadable: {e}"),
            }
        }
        Lists { index, rsp: self.read_rsp() }
    }

    fn read_rsp(&self) -> HashMap<String, String> {
        if !(self.settings.enabled && self.settings.rsp) {
            return HashMap::new();
        }
        let read = match std::fs::read_to_string(self.list_path(&self.rsp_url)) {
            Ok(text) => parse_rsp(&text).map_err(|e| e.to_string()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(HashMap::new()),
            Err(e) => Err(e.to_string()),
        };
        read.unwrap_or_else(|e| {
            tracing::warn!("perennial sources list unreadable: {e}");
            HashMap::new()
        })
    }

    fn current_lists(&self) -> Arc<Lists> {
        self.lists.lock().unwrap_or_else(|e| e.into_inner()).clone().unwrap_or_default()
    }

    /// `source` with the `rsp_*` tags of its host or registrable domain. A config entry or a mark
    /// decides alone.
    fn overlay(&self, mut source: Source) -> Source {
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
        for name in &names {
            if let Some(value) = marks.get(*name) {
                return Some(if value == "allow" || value == "deny" {
                    Source::new(key, "unknown", &[value], "mark")
                } else {
                    Source::new(key, value, &[], "mark")
                });
            }
        }
        let lists = self.current_lists();
        let listed = |name: &str| self.bundled.get(name).or_else(|| lists.index.get(name)).cloned();
        let found = listed(key).or_else(|| listed(registrable(key)))?;
        Some(Source { host: key.to_owned(), ..found })
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

    /// The flight of each of `keys`, starting batches for those with none.
    async fn start(self: &Arc<Self>, keys: Vec<String>) -> HashMap<String, watch::Receiver<()>> {
        let mut seen = HashSet::new();
        let mut out = HashMap::new();
        let mut new = Vec::new();
        let mut flights = self.flights.lock().await;
        for key in keys {
            if !seen.insert(key.clone()) {
                continue;
            }
            if let Some(flight) = flights.get(&key) {
                out.insert(key, flight.clone());
            // A flight that ended after the caller's read has stored its answer by now.
            } else if flights.len() + new.len() < MAX_FLIGHTS && self.cached(&key).await.is_none() {
                new.push(key);
            }
        }
        for batch in new.chunks(BATCH_SIZE) {
            let (done, flight) = watch::channel(());
            for key in batch {
                flights.insert(key.clone(), flight.clone());
                out.insert(key.clone(), flight.clone());
            }
            self.fly(batch.to_vec(), done);
        }
        out
    }

    fn fly(self: &Arc<Self>, batch: Vec<String>, done: watch::Sender<()>) {
        let this = Arc::clone(self);
        tokio::spawn(async move {
            let work = Arc::clone(&this);
            let hosts = batch.clone();
            let lookup = tokio::spawn(async move {
                let _worker = work.workers.acquire().await.expect("never closed");
                work.lookup(hosts).await;
            });
            if let Err(e) = lookup.await {
                tracing::error!("source lookup failed for {} hosts: {e}", batch.len());
            }
            let mut flights = this.flights.lock().await;
            for key in &batch {
                flights.remove(key);
            }
            drop(flights);
            drop(done);
        });
    }

    async fn lookup(&self, mut hosts: Vec<String>) {
        let until = *self.backoff_until.lock().unwrap_or_else(|e| e.into_inner());
        if until.is_some_and(|until| Instant::now() < until) {
            return;
        }
        let pairs: Vec<(String, String)> = hosts.iter().map(|h| (h.clone(), h.clone())).collect();
        let Some(mut matches) = self.ask(&pairs).await else {
            return;
        };
        // The registrable domain is tried only for a host whose own name matched nothing, so a
        // subdomain with an item of its own does not take its parent's.
        let parents: Vec<(String, String)> = hosts
            .iter()
            .filter(|host| !matches.contains_key(*host))
            .map(|host| (host.clone(), registrable(host).to_owned()))
            .filter(|(host, name)| name != host)
            .collect();
        if !parents.is_empty() {
            match self.ask(&parents).await {
                Some(more) => matches.extend(more),
                None => {
                    let skipped: HashSet<&String> = parents.iter().map(|(host, _)| host).collect();
                    hosts.retain(|host| !skipped.contains(host));
                }
            }
        }
        for host in hosts {
            if let Err(e) = self.store.put(&host, matches.remove(&host)).await {
                tracing::warn!("source cache not written for {host}: {e}");
            }
        }
    }

    /// The matches for each (host, name) of `pairs`, or None when the query failed, in which case
    /// the endpoint is left alone for a while.
    async fn ask(&self, pairs: &[(String, String)]) -> Option<HashMap<String, Match>> {
        let body: String = url::form_urlencoded::Serializer::new(String::new())
            .append_pair("query", &query(pairs))
            .finish();
        let sent = self
            .query_client
            .post(&self.settings.endpoint)
            .header(header::USER_AGENT, self.web.user_agent())
            .header(header::ACCEPT, "application/sparql-results+json")
            .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
            .body(body)
            .send()
            .await;
        let response = match sent {
            Ok(response) => response,
            Err(e) => {
                tracing::warn!("wikidata query failed: {e}");
                self.back_off(FAILURE_BACKOFF);
                return None;
            }
        };
        let status = response.status();
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
            let wait = response
                .headers()
                .get(header::RETRY_AFTER)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.trim().parse::<f64>().ok())
                .filter(|wait| wait.is_finite())
                .unwrap_or(DEFAULT_BACKOFF)
                .clamp(0.0, MAX_BACKOFF);
            tracing::warn!("wikidata rate limit hit, backing off {wait:.0} s");
            self.back_off(wait);
            return None;
        }
        if !status.is_success() {
            tracing::warn!("wikidata query answered {}", status.as_u16());
            self.back_off(FAILURE_BACKOFF);
            return None;
        }
        match response.json::<Answer>().await {
            Ok(answer) => Some(parse(answer)),
            Err(e) => {
                tracing::warn!("wikidata answer unreadable: {e}");
                None
            }
        }
    }

    fn back_off(&self, seconds: f64) {
        let until = Instant::now() + Duration::from_secs_f64(seconds);
        *self.backoff_until.lock().unwrap_or_else(|e| e.into_inner()) = Some(until);
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

/// The hosts in a hosts file, a uBlacklist (`*://*.example.com/*`) or a CSV whose first column is
/// the host. Comments, regexes and rules for part of a site are skipped.
pub fn parse_list(text: &str) -> HashSet<String> {
    let mut hosts = HashSet::new();
    // What Python's `splitlines` breaks on: some lists end lines in a lone `\r`.
    let breaks =
        ['\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\u{85}', '\u{2028}', '\u{2029}'];
    for line in text.split(breaks) {
        // A comment after the host is cut; a hosts file has the address first and the host second.
        let line = COMMENT.split(line.trim()).next().unwrap_or("");
        let line = line.split(',').next().unwrap_or("").trim();
        if line.is_empty() || line.starts_with(['#', '!', '/']) {
            continue;
        }
        let words: Vec<&str> = line.split_whitespace().collect();
        let token = words[1.min(words.len() - 1)];
        let token = token.strip_prefix("*://").unwrap_or(token);
        let token = token.strip_prefix("*.").unwrap_or(token);
        let (name, path) = token.split_once('/').unwrap_or((token, ""));
        if !matches!(path, "" | "*") {
            continue;
        }
        if let Some(host) = normalize(name) {
            hosts.insert(host);
        }
    }
    hosts
}

#[derive(Deserialize)]
struct RspAnswer {
    query: RspQuery,
}

#[derive(Deserialize)]
struct RspQuery {
    pages: Vec<RspPage>,
}

#[derive(Deserialize)]
struct RspPage {
    #[serde(default)]
    revisions: Vec<RspRevision>,
}

#[derive(Deserialize)]
struct RspRevision {
    slots: RspSlots,
}

#[derive(Deserialize)]
struct RspSlots {
    main: RspMain,
}

#[derive(Deserialize)]
struct RspMain {
    content: String,
}

/// The `rsp_*` tag of each site on the perennial sources page, from the JSON the API answers with.
/// A row is `|- class="s-<status>"` and its sites are in `{{WP:RSPUSES|a.com|b.com}}`. A site in
/// several rows (a topic with a status of its own) takes the least reliable. Rows with no site, a
/// status that is none of the five, a part of a site (`a.com/news`) or a site in `RSP_SKIP` are
/// skipped.
pub fn parse_rsp(text: &str) -> Result<HashMap<String, String>, serde_json::Error> {
    let answer: RspAnswer = serde_json::from_str(text)?;
    let mut found: HashMap<String, String> = HashMap::new();
    for page in answer.query.pages {
        let Some(revision) = page.revisions.first() else {
            continue;
        };
        let wikitext = &revision.slots.main.content;
        let starts: Vec<usize> = RSP_ROW.find_iter(wikitext).map(|row| row.start()).collect();
        for (i, start) in starts.iter().enumerate() {
            let row = &wikitext[*start..starts.get(i + 1).copied().unwrap_or(wikitext.len())];
            let Some(status) = RSP_STATUS.captures(row) else {
                continue;
            };
            let Some(tag) = rsp_tag(&status[1]) else {
                continue;
            };
            for uses in RSP_USES.captures_iter(row) {
                for raw in uses[1].split('|') {
                    let (name, path) = raw.split_once('/').unwrap_or((raw, ""));
                    let Some(host) = normalize(name) else {
                        continue;
                    };
                    if RSP_SKIP.contains(&host.as_str()) || !matches!(path, "" | "*") {
                        continue;
                    }
                    let rank = |tag: &str| RSP_ORDER.iter().position(|known| *known == tag);
                    let held = found.get(&host).and_then(|held| rank(held));
                    if held.is_none() || rank(tag) > held {
                        found.insert(host, tag.to_owned());
                    }
                }
            }
        }
    }
    Ok(found)
}

fn rsp_tag(status: &str) -> Option<&'static str> {
    match status {
        "gr" => Some("rsp_reliable"),
        "nc" => Some("rsp_mixed"),
        "gu" => Some("rsp_unreliable"),
        "d" | "b" => Some("rsp_deprecated"),
        _ => None,
    }
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

/// Writes a list so that a reader never sees half of it, and it survives a crash once renamed.
fn save(dir: &Path, path: &Path, body: &[u8]) -> io::Result<()> {
    std::fs::create_dir_all(dir)?;
    let mut temp = tempfile::NamedTempFile::new_in(dir)?;
    temp.write_all(body)?;
    temp.as_file().sync_all()?;
    temp.persist(path).map_err(|e| e.error)?;
    std::fs::File::open(dir)?.sync_all()
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

fn query(pairs: &[(String, String)]) -> String {
    let (mut names, mut urls) = (Vec::new(), Vec::new());
    for (host, name) in pairs {
        names.push(format!("(\"{host}\" \"{name}\")"));
        for scheme in ["http", "https"] {
            for www in ["", "www."] {
                for slash in ["", "/"] {
                    urls.push(format!("(\"{host}\" <{scheme}://{www}{name}{slash}>)"));
                }
            }
        }
    }
    let roots = ROOT_KINDS
        .iter()
        .chain(ROOT_TAGS.iter())
        .map(|(root, _)| *root)
        .chain([INTERNATIONAL, NON_GOVERNMENTAL])
        .map(|root| format!("wd:{root}"))
        .collect::<Vec<_>>()
        .join(" ");
    format!(
        "SELECT ?h ?i ?l ?root WHERE {{\n  VALUES ?root {{ {roots} }}\n  \
         {{ VALUES (?h ?d) {{ {} }} ?i wdt:P13337 ?d }}\n  UNION\n  \
         {{ VALUES (?h ?u) {{ {} }} ?i wdt:P856 ?u }}\n  ?i wdt:P31/wdt:P279* ?root .\n  \
         OPTIONAL {{ ?i rdfs:label ?l FILTER(LANG(?l) = \"en\") }}\n}} GROUP BY ?h ?i ?l ?root",
        names.join(" "),
        urls.join(" "),
    )
}

#[derive(Deserialize)]
struct Answer {
    results: Results,
}

#[derive(Deserialize)]
struct Results {
    bindings: Vec<Binding>,
}

#[derive(Deserialize)]
struct Binding {
    h: Value,
    i: Value,
    root: Value,
    l: Option<Value>,
}

#[derive(Deserialize)]
struct Value {
    value: String,
}

fn local(value: &str) -> &str {
    value.strip_prefix(NAMESPACE).unwrap_or(value)
}

/// The matches in a WDQS JSON answer, by host. A host takes the best kind across its items.
fn parse(answer: Answer) -> HashMap<String, Match> {
    // Items in the order they came, so of two with one kind the first gives the label.
    type Items = Vec<(String, HashSet<String>)>;
    let mut roots: HashMap<String, Items> = HashMap::new();
    let mut labels: HashMap<String, String> = HashMap::new();
    for row in answer.results.bindings {
        let item = local(&row.i.value).to_owned();
        let items = roots.entry(row.h.value).or_default();
        let at = match items.iter().position(|(known, _)| *known == item) {
            Some(at) => at,
            None => {
                items.push((item.clone(), HashSet::new()));
                items.len() - 1
            }
        };
        items[at].1.insert(local(&row.root.value).to_owned());
        if let Some(label) = row.l {
            labels.insert(item, label.value);
        }
    }
    let rank = |kind: &str| KINDS.iter().position(|known| *known == kind).unwrap_or(KINDS.len());
    let mut out = HashMap::new();
    for (host, items) in roots {
        let mut best = ("unknown", String::new());
        let mut tags = BTreeSet::from(["official".to_owned()]);
        for (item, reached) in items {
            let mut kinds: Vec<&str> = ROOT_KINDS
                .iter()
                .filter(|(root, _)| reached.contains(*root))
                .map(|(_, kind)| *kind)
                .collect();
            if reached.contains(INTERNATIONAL) && !reached.contains(NON_GOVERNMENTAL) {
                kinds.push("intergovernmental");
            }
            tags.extend(
                ROOT_TAGS
                    .iter()
                    .filter(|(root, _)| reached.contains(*root))
                    .map(|(_, tag)| (*tag).to_owned()),
            );
            for kind in kinds {
                if rank(kind) < rank(best.0) {
                    best = (kind, item.clone());
                }
            }
        }
        let label = labels.get(&best.1).cloned().unwrap_or_default();
        out.insert(host, Match { kind: best.0.to_owned(), tags, label, qid: best.1 });
    }
    out
}

#[cfg(test)]
#[path = "sources_tests.rs"]
mod tests;
