//! The downloaded host lists: fetched on a TTL, kept on disk, and parsed into an index.

use std::collections::{HashMap, HashSet};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};
use std::time::{Duration, SystemTime};

use kvasir_types::sites::{KINDS, LIST_TAGS};
use regex::Regex;
use reqwest::header;
use sha1::{Digest, Sha1};
use tokio::time::Instant;

use super::{Source, Sources, normalize, parse_rsp};

// How often the downloaded lists are checked for age.
pub(super) const LIST_CHECK: Duration = Duration::from_secs(3600);
pub(super) static COMMENT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\s#").expect("a valid pattern"));

/// What the downloaded lists say, as last read from disk.
#[derive(Default)]
pub(super) struct Lists {
    pub(super) index: HashMap<String, Source>,
    pub(super) rsp: HashMap<String, String>,
}

impl Sources {
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

    pub(super) fn list_entries(&self) -> Vec<String> {
        let mut entries = self.settings.lists.clone();
        if self.settings.enabled && self.settings.rsp {
            entries.push(format!("rsp={}", self.rsp_url));
        }
        entries
    }

    pub(super) async fn check_lists(self: &Arc<Self>) {
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

    pub(super) fn list_path(&self, url: &str) -> PathBuf {
        let digest: String =
            Sha1::digest(url.as_bytes()).iter().map(|byte| format!("{byte:02x}")).collect();
        self.data_dir.join(format!("sources-{}.txt", &digest[..12]))
    }

    pub(super) async fn load_lists(self: &Arc<Self>) {
        let this = Arc::clone(self);
        let lists =
            tokio::task::spawn_blocking(move || this.read_lists()).await.unwrap_or_default();
        *self.lists.lock().unwrap_or_else(|e| e.into_inner()) = Some(Arc::new(lists));
    }

    pub(super) fn read_lists(&self) -> Lists {
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

    pub(super) fn current_lists(&self) -> Arc<Lists> {
        self.lists.lock().unwrap_or_else(|e| e.into_inner()).clone().unwrap_or_default()
    }
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

/// Writes a list so that a reader never sees half of it, and it survives a crash once renamed.
pub(super) fn save(dir: &Path, path: &Path, body: &[u8]) -> io::Result<()> {
    std::fs::create_dir_all(dir)?;
    let mut temp = tempfile::NamedTempFile::new_in(dir)?;
    temp.write_all(body)?;
    temp.as_file().sync_all()?;
    temp.persist(path).map_err(|e| e.error)?;
    std::fs::File::open(dir)?.sync_all()
}
