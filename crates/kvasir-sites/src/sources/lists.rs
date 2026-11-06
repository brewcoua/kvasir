//! The downloaded host lists: fetched on a TTL, kept on disk, and parsed into an index.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};
use std::time::{Duration, SystemTime};

use kvasir_types::sites::{KINDS, LIST_TAGS, ListHit, SourceList, SourceListInfo};
use regex::Regex;
use reqwest::header;
use sha1::{Digest, Sha1};
use tokio::time::Instant;

use super::survey::RANKED;
use super::{DbError, Sources, normalize, parse_rsp, parse_tranco};

const APPLIES: &str = "applies";
// How often the downloaded lists are checked for age.
pub(super) const LIST_CHECK: Duration = Duration::from_secs(3600);
pub(super) static COMMENT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\s#").expect("a valid pattern"));

/// What the downloaded lists say, as last read from disk.
#[derive(Default)]
pub(super) struct Lists {
    pub(super) listed: Vec<Listed>,
    pub(super) rsp: HashMap<String, String>,
    pub(super) popular: HashSet<String>,
    /// The sites a guarded list spares for their rank.
    pub(super) top: HostSet,
}

/// What a list does to the sites it names.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum Effect {
    Block,
    Demote,
    Exempt,
    Trust,
    Kind(String),
    /// From `sources.lists`, whose tag sets the tier itself.
    Tag(String),
}

impl Effect {
    fn parse(effect: &str) -> Option<Self> {
        match effect {
            "block" => Some(Self::Block),
            "demote" => Some(Self::Demote),
            "exempt" => Some(Self::Exempt),
            "trust" => Some(Self::Trust),
            _ => effect.strip_prefix("kind:").map(|kind| Self::Kind(kind.to_owned())),
        }
    }

    fn label(&self) -> String {
        match self {
            Self::Block => "block".to_owned(),
            Self::Demote => "demote".to_owned(),
            Self::Exempt => "exempt".to_owned(),
            Self::Trust => "trust".to_owned(),
            Self::Kind(kind) => format!("kind:{kind}"),
            Self::Tag(tag) => format!("tag:{tag}"),
        }
    }
}

/// One list, read from disk or from its entries. A list that is off is held too: a run may turn it
/// on for itself.
pub(super) struct Listed {
    pub(super) id: String,
    pub(super) name: String,
    pub(super) enabled: bool,
    pub(super) effect: Effect,
    pub(super) tag: String,
    pub(super) guard: bool,
    pub(super) except: HostSet,
    pub(super) hosts: HostSet,
    pub(super) skipped: usize,
}

impl Listed {
    fn from_label(at: usize, entry: &str, label: &str, parsed: Parsed) -> Option<Self> {
        let effect = if LIST_TAGS.contains(&label) {
            Effect::Tag(label.to_owned())
        } else if KINDS[..KINDS.len() - 1].contains(&label) {
            Effect::Kind(label.to_owned())
        } else {
            tracing::warn!("source list {entry:?} ignored: its label is not a kind or tag");
            return None;
        };
        Some(Self {
            id: config_id(at),
            name: label.to_owned(),
            enabled: true,
            effect,
            tag: String::new(),
            guard: false,
            except: HostSet::default(),
            hosts: HostSet::new(parsed.hosts),
            skipped: parsed.skipped,
        })
    }

    fn lowers(&self) -> bool {
        matches!(self.effect, Effect::Block | Effect::Demote)
    }

    /// Whether this list names a site known by `names`, and if so, whether it counts.
    fn state(&self, names: &[&str], guarded: bool) -> Option<&'static str> {
        if !names.iter().any(|name| self.hosts.contains(name)) {
            return None;
        }
        Some(if !self.enabled {
            "off"
        } else if names.iter().any(|name| self.except.contains(name)) {
            "except"
        } else if guarded && self.guard && self.lowers() {
            "guard"
        } else {
            APPLIES
        })
    }
}

/// One download: a list's name, where from, and how long a copy on disk lasts.
pub(super) struct Fetch {
    pub(super) name: String,
    url: String,
    ttl_days: u64,
}

/// Sorted hosts in one string. A fraction of a `HashSet<String>`'s memory, for a slower lookup that
/// a search cannot notice: lists run to tens of thousands of hosts, and a site is asked about once.
#[derive(Default)]
pub(super) struct HostSet {
    text: String,
    ends: Vec<u32>,
}

impl HostSet {
    pub(super) fn new(hosts: impl IntoIterator<Item = String>) -> Self {
        let mut hosts: Vec<String> = hosts.into_iter().collect();
        hosts.sort_unstable();
        let mut set = Self {
            text: String::with_capacity(hosts.iter().map(String::len).sum()),
            ends: Vec::with_capacity(hosts.len()),
        };
        for host in hosts {
            set.text.push_str(&host);
            // A list is under `MAX_LIST_BYTES`, so its text fits.
            set.ends.push(set.text.len() as u32);
        }
        set
    }

    fn get(&self, at: usize) -> &str {
        let start = at.checked_sub(1).map_or(0, |before| self.ends[before] as usize);
        &self.text[start..self.ends[at] as usize]
    }

    pub(super) fn contains(&self, host: &str) -> bool {
        let (mut low, mut high) = (0, self.ends.len());
        while low < high {
            let middle = low + (high - low) / 2;
            match self.get(middle).cmp(host) {
                Ordering::Less => low = middle + 1,
                Ordering::Greater => high = middle,
                Ordering::Equal => return true,
            }
        }
        false
    }
}

impl Lists {
    /// The lists that apply to a site known by `names`. `guarded` says a guard spares it from
    /// what a list would lower it by, for its suffix; its rank is checked here.
    pub(super) fn hits(&self, names: &[&str], guarded: bool) -> Vec<&Listed> {
        let guarded = guarded || names.iter().any(|name| self.top.contains(name));
        self.listed.iter().filter(|list| list.state(names, guarded) == Some(APPLIES)).collect()
    }

    /// Every list naming a site known by `names`, and whether it counts.
    pub(super) fn explain(&self, names: &[&str], guarded: bool) -> Vec<ListHit> {
        let guarded = guarded || names.iter().any(|name| self.top.contains(name));
        let hit = |list: &Listed| {
            list.state(names, guarded).map(|state| ListHit {
                id: list.id.clone(),
                name: list.name.clone(),
                effect: list.effect.label(),
                state: state.to_owned(),
            })
        };
        self.listed.iter().filter_map(hit).collect()
    }
}

impl Sources {
    /// Downloads each list, and with `rsp` the perennial sources page, that is missing or older than
    /// `ttl_days`, then rebuilds the index. One that fails to download keeps the copy on disk.
    pub async fn refresh_lists(self: &Arc<Self>) {
        *self.lists_checked.lock().unwrap_or_else(|e| e.into_inner()) = Some(Instant::now());
        self.load_specs().await;
        for Fetch { name, url, ttl_days } in self.fetches() {
            let path = self.list_path(&url);
            let age = std::fs::metadata(&path)
                .and_then(|meta| meta.modified())
                .ok()
                .and_then(|modified| SystemTime::now().duration_since(modified).ok());
            let ttl = Duration::from_secs(ttl_days.saturating_mul(86400));
            if age.is_some_and(|age| age < ttl) {
                continue;
            }
            self.fetch(&name, &url, &path).await;
        }
        self.load_lists().await;
    }

    /// Downloads `url` to `path`, keeping why it failed for the list's status.
    async fn fetch(&self, name: &str, url: &str, path: &Path) {
        let done = self.download(url, path).await;
        let mut errors = self.list_errors.lock().unwrap_or_else(|e| e.into_inner());
        match done {
            Ok(()) => drop(errors.remove(url)),
            Err(e) => {
                tracing::warn!("source list {name} not fetched ({url}): {e}");
                errors.insert(url.to_owned(), e);
            }
        }
    }

    /// Downloads one list now, however young its copy, and reads it in.
    pub async fn refresh_list(
        self: &Arc<Self>,
        id: &str,
    ) -> Result<SourceListInfo, super::ListError> {
        let lists = self.lists().await?;
        let Some(list) = lists.into_iter().find(|list| list.id == id) else {
            return Err(super::ListError::Missing(id.to_owned()));
        };
        if let Some(url) = &list.url {
            self.fetch(&list.name, url, &self.list_path(url)).await;
        }
        self.load_lists().await;
        Ok(self.status(list))
    }

    /// Every list with its status: the catalog's and the operator's, then those in the config.
    pub async fn list_statuses(&self) -> Result<Vec<SourceListInfo>, DbError> {
        let mut lists: Vec<SourceList> = self.lists().await?;
        lists.extend(self.settings.lists.iter().enumerate().map(|(at, entry)| {
            let (label, url) = entry.split_once('=').unwrap_or((entry, ""));
            let effect = if LIST_TAGS.contains(&label) { "tag" } else { "kind" };
            SourceList {
                id: config_id(at),
                name: label.to_owned(),
                url: Some(url.to_owned()),
                entries: Vec::new(),
                enabled: true,
                effect: format!("{effect}:{label}"),
                tag: String::new(),
                ttl_days: None,
                guard: false,
                except: Vec::new(),
                group: "Config".to_owned(),
                licence: String::new(),
                custom: false,
            }
        }));
        Ok(lists.into_iter().map(|list| self.status(list)).collect())
    }

    pub(super) fn status(&self, list: SourceList) -> SourceListInfo {
        let lists = self.current_lists();
        let read = lists.listed.iter().find(|read| read.id == list.id);
        let url = list.url.as_deref();
        let fetched_at = url
            .and_then(|url| std::fs::metadata(self.list_path(url)).ok())
            .and_then(|meta| meta.modified().ok())
            .and_then(|at| at.duration_since(SystemTime::UNIX_EPOCH).ok())
            .map(|age| age.as_secs());
        let errors = self.list_errors.lock().unwrap_or_else(|e| e.into_inner());
        let error = url.and_then(|url| errors.get(url)).cloned();
        SourceListInfo {
            hosts: read.map_or(0, |read| read.hosts.ends.len() as u64),
            skipped: read.map_or(0, |read| read.skipped as u64),
            fetched_at,
            error,
            list,
        }
    }

    pub(super) async fn download(&self, url: &str, path: &Path) -> Result<(), String> {
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

    /// Every download that should be kept current.
    pub(super) fn fetches(&self) -> Vec<Fetch> {
        let fetch = |name: &str, url: &str, ttl_days: Option<u64>| Fetch {
            name: name.to_owned(),
            url: url.to_owned(),
            ttl_days: ttl_days.unwrap_or(self.settings.ttl_days),
        };
        let mut fetches: Vec<Fetch> = self
            .settings
            .lists
            .iter()
            .map(|entry| {
                let (label, url) = entry.split_once('=').unwrap_or((entry, ""));
                fetch(label, url, None)
            })
            .collect();
        for list in self.current_specs().iter().filter(|list| list.enabled) {
            if let Some(url) = &list.url {
                fetches.push(fetch(&list.name, url, list.ttl_days));
            }
        }
        if self.settings.enabled && self.settings.rsp {
            fetches.push(fetch("rsp", &self.rsp_url, None));
        }
        if self.settings.enabled && (self.settings.survey || self.guard_applies()) {
            fetches.push(fetch("tranco", &self.tranco_url, None));
        }
        fetches
    }

    /// Whether any list on needs the Tranco list for its guard.
    fn guard_applies(&self) -> bool {
        self.settings.guard_rank > 0
            && self.current_specs().iter().any(|list| {
                list.enabled && list.guard && matches!(list.effect.as_str(), "block" | "demote")
            })
    }

    pub(super) fn current_specs(&self) -> Arc<Vec<SourceList>> {
        self.specs.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    pub(super) async fn load_specs(&self) {
        match self.lists().await {
            Ok(specs) => *self.specs.lock().unwrap_or_else(|e| e.into_inner()) = Arc::new(specs),
            Err(e) => tracing::warn!("source lists unreadable, keeping the last read: {e}"),
        }
    }

    pub(super) async fn check_lists(self: &Arc<Self>) {
        // Held across the read, so callers at startup parse the lists once between them.
        let first_load = self.first_load.lock().await;
        let loaded = self.lists.lock().unwrap_or_else(|e| e.into_inner()).is_some();
        if !loaded {
            self.load_lists().await;
        }
        drop(first_load);
        if self.fetches().is_empty() {
            return;
        }
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

    pub(super) fn list_path(&self, url: &str) -> PathBuf {
        let digest: String =
            Sha1::digest(url.as_bytes()).iter().map(|byte| format!("{byte:02x}")).collect();
        self.data_dir.join(format!("sources-{}.txt", &digest[..12]))
    }

    pub(super) async fn load_lists(self: &Arc<Self>) {
        self.load_specs().await;
        let this = Arc::clone(self);
        let lists =
            tokio::task::spawn_blocking(move || this.read_lists()).await.unwrap_or_default();
        *self.lists.lock().unwrap_or_else(|e| e.into_inner()) = Some(Arc::new(lists));
        if self.settings.survey {
            self.load_flags().await;
        }
    }

    pub(super) fn read_lists(&self) -> Lists {
        let mut listed = Vec::new();
        for (at, entry) in self.settings.lists.iter().enumerate() {
            let (label, url) = entry.split_once('=').unwrap_or((entry, ""));
            if let Some(parsed) = self.read_hosts(label, url) {
                listed.extend(Listed::from_label(at, entry, label, parsed));
            }
        }
        for list in self.current_specs().iter() {
            let parsed = match &list.url {
                Some(url) => self.read_hosts(&list.name, url),
                None => Some(Parsed { hosts: list.entries.iter().cloned().collect(), skipped: 0 }),
            };
            let (Some(parsed), Some(effect)) = (parsed, Effect::parse(&list.effect)) else {
                continue;
            };
            listed.push(Listed {
                id: list.id.clone(),
                name: list.name.clone(),
                enabled: list.enabled,
                effect,
                tag: list.tag.clone(),
                guard: list.guard,
                except: HostSet::new(list.except.iter().cloned()),
                hosts: HostSet::new(parsed.hosts),
                skipped: parsed.skipped,
            });
        }
        let (popular, top) = self.read_tranco();
        Lists { listed, rsp: self.read_rsp(), popular, top }
    }

    /// The hosts of the copy on disk of the list at `url`, if there is one that can be read.
    fn read_hosts(&self, name: &str, url: &str) -> Option<Parsed> {
        match std::fs::read_to_string(self.list_path(url)) {
            Ok(text) => {
                let parsed = parse_list(&text);
                tracing::info!(
                    "source list {name}: {} hosts, {} rules skipped",
                    parsed.hosts.len(),
                    parsed.skipped
                );
                Some(parsed)
            }
            // Not downloaded yet: the first refresh runs after the first load.
            Err(e) if e.kind() == io::ErrorKind::NotFound => None,
            Err(e) => {
                tracing::warn!("source list {name} unreadable: {e}");
                None
            }
        }
    }

    pub(super) fn read_rsp(&self) -> HashMap<String, String> {
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

    /// What the survey and the guard need of the Tranco list, each only when it is on.
    fn read_tranco(&self) -> (HashSet<String>, HostSet) {
        let survey = self.settings.enabled && self.settings.survey;
        let guard = self.settings.enabled && self.guard_applies();
        if !survey && !guard {
            return Default::default();
        }
        let zip = match std::fs::read(self.list_path(&self.tranco_url)) {
            Ok(zip) => zip,
            Err(e) => {
                if e.kind() != io::ErrorKind::NotFound {
                    tracing::warn!("Tranco list unreadable: {e}");
                }
                return Default::default();
            }
        };
        let parse = |top: usize| {
            parse_tranco(&zip, top).unwrap_or_else(|e| {
                tracing::warn!("Tranco list unreadable: {e}");
                HashSet::new()
            })
        };
        let popular = if survey { parse(RANKED) } else { HashSet::new() };
        let top = if guard {
            parse(usize::try_from(self.settings.guard_rank).unwrap_or(usize::MAX))
        } else {
            HashSet::new()
        };
        (popular, HostSet::new(top))
    }

    pub(super) fn current_lists(&self) -> Arc<Lists> {
        self.lists.lock().unwrap_or_else(|e| e.into_inner()).clone().unwrap_or_default()
    }
}

/// The id shown for the `at`th entry of `sources.lists`.
fn config_id(at: usize) -> String {
    format!("config-{at}")
}

/// What a list holds.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Parsed {
    pub hosts: HashSet<String>,
    /// Lines that are not a comment or a whole site: regexes, and rules for part of one.
    pub skipped: usize,
}

/// The hosts in a hosts file, a uBlacklist (`*://*.example.com/*`), an AdBlock list
/// (`||example.com^`) or a CSV whose first column is the host.
pub fn parse_list(text: &str) -> Parsed {
    let mut parsed = Parsed::default();
    // What Python's `splitlines` breaks on: some lists end lines in a lone `\r`.
    let breaks =
        ['\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\u{85}', '\u{2028}', '\u{2029}'];
    for line in text.split(breaks) {
        // A comment after the host is cut; a hosts file has the address first and the host second.
        let line = COMMENT.split(line.trim()).next().unwrap_or("");
        let line = line.split(',').next().unwrap_or("").trim();
        if line.is_empty() || line.starts_with(['#', '!']) {
            continue;
        }
        if let Some(rule) = line.strip_prefix("||") {
            // Other options narrow the rule to some requests, so it does not speak for the site.
            let name = ["^", "^$document", "^$all"].iter().find_map(|end| rule.strip_suffix(end));
            match name.and_then(normalize) {
                Some(host) => drop(parsed.hosts.insert(host)),
                None => parsed.skipped += 1,
            }
            continue;
        }
        if line.starts_with('/') {
            parsed.skipped += 1;
            continue;
        }
        let words: Vec<&str> = line.split_whitespace().collect();
        let token = words[1.min(words.len() - 1)];
        let token = token.strip_prefix("*://").unwrap_or(token);
        let token = token.strip_prefix("*.").unwrap_or(token);
        let (name, path) = token.split_once('/').unwrap_or((token, ""));
        match normalize(name).filter(|_| matches!(path, "" | "*")) {
            Some(host) => drop(parsed.hosts.insert(host)),
            None => parsed.skipped += 1,
        }
    }
    parsed
}

/// Writes a list so that a reader never sees half of it, and it survives a crash once renamed.
pub(super) fn save(dir: &Path, path: &Path, body: &[u8]) -> io::Result<()> {
    std::fs::create_dir_all(dir)?;
    let mut temp = tempfile::NamedTempFile::new_in(dir)?;
    temp.write_all(body)?;
    temp.as_file().sync_all()?;
    temp.persist(path).map_err(|e| e.error)?;
    std::fs::File::open(dir)?.sync_all()
}
