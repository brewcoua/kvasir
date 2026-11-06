//! Tags a site as a likely content farm from how fast its sitemap says it publishes and how few
//! visit it. See `docs/sources-plan.md`, phase 7, for what was measured and why these two.
//!
//! This is evidence and not proof, so the tag lowers a site and does not block it. Only a site a run
//! met and nothing else decides is surveyed, so an `avoid` site is never read for it.

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Cursor};
use std::sync::atomic::Ordering;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use kvasir_types::sites::{Tier, registrable};
use regex::Regex;
use reqwest::header;
use tokio::time::timeout;
use url::Url;

use super::{Source, Sources, normalize};

pub(super) const TRANCO_URL: &str = "https://tranco-list.eu/top-1m.csv.zip";
pub(super) const LIKELY_FARM: &str = "likely_farm";
// Past this rank a site counts as unvisited. Real small sites sit below it too, so this alone
// tags nothing.
pub(super) const RANKED: usize = 300_000;
const MIN_DATED: usize = 100;
const DENSE_PER_DAY: f64 = 5.0;
const BATCH: usize = 20;
const MAX_FILES: usize = 8;
const MAX_FILE_BYTES: usize = 8 * 1024 * 1024;
const MAX_REDIRECTS: usize = 5;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
// Sitemaps name their parts, and a hostile index must not cost more than the files read.
const MAX_QUEUED: usize = 64;

static LASTMOD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"<lastmod>\s*(\d{4}-\d{2}-\d{2})").expect("a valid pattern"));
static LOC: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)").expect("a valid pattern")
});

/// What a site's sitemap says.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Survey {
    pub urls: usize,
    pub dated: usize,
    /// URLs a publishing day; see `rate`.
    pub rate: Option<f64>,
}

/// URLs per day from their `lastmod` dates, leaving out the busiest day: a site that regenerates
/// its sitemap stamps most of it with one date, which says nothing of when it wrote them. `None`
/// with too few dates to tell.
pub fn rate(stamps: &[String]) -> Option<f64> {
    if stamps.len() < MIN_DATED {
        return None;
    }
    let mut days: HashMap<&str, usize> = HashMap::new();
    for stamp in stamps {
        *days.entry(stamp).or_default() += 1;
    }
    let busiest = days.values().copied().max().unwrap_or(0);
    let others = days.len().checked_sub(1).filter(|others| *others > 0)?;
    Some((stamps.len() - busiest) as f64 / others as f64)
}

/// Why the survey tags a site, or `None` when it does not. `ranked` is false for a site past
/// `RANKED`.
pub fn verdict(survey: &Survey, ranked: bool) -> Option<String> {
    let rate = survey.rate.filter(|rate| *rate >= DENSE_PER_DAY && !ranked)?;
    let dated = survey.dated;
    Some(format!("{rate:.1} URLs a day over {dated} dated, outside Tranco's top {RANKED}"))
}

/// The top `top` hosts of a Tranco zip, whose one file has `rank,host` lines in rank order.
pub fn parse_tranco(zip: &[u8], top: usize) -> Result<HashSet<String>, String> {
    let mut archive = zip::ZipArchive::new(Cursor::new(zip)).map_err(|e| e.to_string())?;
    let file = archive.by_index(0).map_err(|e| e.to_string())?;
    let mut hosts = HashSet::new();
    for line in BufReader::new(file).lines().take(top) {
        let line = line.map_err(|e| e.to_string())?;
        if let Some(host) = line.split_once(',').and_then(|(_, host)| normalize(host)) {
            hosts.insert(host);
        }
    }
    Ok(hosts)
}

/// The sitemaps a robots.txt names.
fn named_sitemaps(robots: &str) -> Vec<String> {
    robots
        .lines()
        .filter_map(|line| line.split_once(':'))
        .filter(|(name, _)| name.trim().eq_ignore_ascii_case("sitemap"))
        .map(|(_, url)| url.trim().to_owned())
        .collect()
}

impl Sources {
    pub(super) fn current_flags(&self) -> Arc<HashMap<String, String>> {
        self.flags.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    pub(super) async fn load_flags(&self) {
        let flags = self.store.flagged().await.unwrap_or_else(|e| {
            tracing::warn!("site surveys unreadable: {e}");
            HashMap::new()
        });
        *self.flags.lock().unwrap_or_else(|e| e.into_inner()) = Arc::new(flags);
    }

    /// `source` with the survey's tag, when nothing else says what it is.
    pub(super) fn flag(&self, mut source: Source) -> Source {
        if source.tier() != Tier::Unknown || !source.tags.is_empty() {
            return source;
        }
        if let Some(evidence) = self.current_flags().get(registrable(&source.host)) {
            source.tags.insert(LIKELY_FARM.to_owned());
            source.evidence.clone_from(evidence);
            source.origin = "survey".to_owned();
        }
        source
    }

    /// Queues the sites of `found` nothing decides, then starts the job unless it is running.
    pub(super) async fn note_met<'a>(self: &Arc<Self>, found: impl Iterator<Item = &'a Source>) {
        let met: HashSet<String> = found
            .filter(|source| source.tier() == Tier::Unknown && source.tags.is_empty())
            .filter(|source| !source.host.is_empty() && normalize(&source.host).is_some())
            .map(|source| registrable(&source.host).to_owned())
            .collect();
        if !met.is_empty()
            && let Err(e) = self.store.note_met(met.into_iter().collect()).await
        {
            tracing::warn!("site survey queue unwritable: {e}");
        }
        if !self.surveying.swap(true, Ordering::SeqCst) {
            let this = Arc::clone(self);
            tokio::spawn(async move {
                this.survey_due().await;
                this.surveying.store(false, Ordering::SeqCst);
            });
        }
    }

    /// Surveys one batch of the sites new or past `ttl_days`. The next classification starts the
    /// next batch, so a service nobody uses sends nothing.
    pub(super) async fn survey_due(self: &Arc<Self>) {
        let lists = self.current_lists();
        // Without the list every site would look unvisited.
        if lists.popular.is_empty() {
            return;
        }
        let due = match self.store.due(self.settings.ttl_days, BATCH).await {
            Ok(due) => due,
            Err(e) => {
                tracing::warn!("site survey queue unreadable: {e}");
                return;
            }
        };
        for host in due {
            let marks = self.marks_of(&[&host]).await;
            let source = match self.fixed(&host, &marks) {
                Some(source) => Some(source),
                None => self.answered(&host).await,
            };
            let Some(source) = source.map(|source| self.rsp(source)) else {
                // Wikidata might call it AI-generated, and an `avoid` site is not read: ask first.
                self.start(vec![host]).await;
                continue;
            };
            let survey = if source.tier() == Tier::Unknown && source.tags.is_empty() {
                self.read_sitemaps(&host).await
            } else {
                Survey::default()
            };
            let evidence = verdict(&survey, lists.popular.contains(&host));
            if let Err(e) = self.store.record(&host, &survey, evidence.as_deref()).await {
                tracing::warn!("site survey of {host} not kept: {e}");
                continue;
            }
            if let Some(evidence) = evidence {
                tracing::info!("site survey: {host} is a likely farm: {evidence}");
                let mut flags = self.flags.lock().unwrap_or_else(|e| e.into_inner());
                Arc::make_mut(&mut flags).insert(host, evidence);
            } else {
                let mut flags = self.flags.lock().unwrap_or_else(|e| e.into_inner());
                if flags.contains_key(&host) {
                    Arc::make_mut(&mut flags).remove(&host);
                }
            }
        }
    }

    /// What the rules and the cache say of `host`, or `None` while Wikidata has yet to answer.
    async fn answered(&self, host: &str) -> Option<Source> {
        let entry = self.cached(host).await;
        if entry.is_none() && self.settings.wikidata {
            return None;
        }
        Some(super::merge(host, super::rule(host), entry.flatten()))
    }

    async fn read_sitemaps(&self, host: &str) -> Survey {
        let base = self.survey_base.clone().unwrap_or_else(|| format!("https://{host}"));
        let robots = self.read_text(&format!("{base}/robots.txt"), &base).await.unwrap_or_default();
        let mut queue = named_sitemaps(&robots);
        if queue.is_empty() {
            queue = vec![format!("{base}/sitemap.xml"), format!("{base}/sitemap_index.xml")];
        }
        let mut seen = HashSet::new();
        let mut stamps = Vec::new();
        let mut urls = 0;
        let mut read = 0;
        while read < MAX_FILES && !queue.is_empty() {
            let url = queue.remove(0);
            if !seen.insert(url.clone()) {
                continue;
            }
            read += 1;
            let Some(text) = self.read_text(&url, &base).await else {
                continue;
            };
            if text.contains("<sitemapindex") {
                let parts = LOC.captures_iter(&text).map(|found| found[1].to_owned());
                let room = MAX_QUEUED.saturating_sub(queue.len());
                queue.extend(parts.take(room));
                continue;
            }
            urls += text.matches("<url>").count();
            stamps.extend(LASTMOD.captures_iter(&text).map(|found| found[1].to_owned()));
        }
        Survey { urls, dated: stamps.len(), rate: rate(&stamps) }
    }

    /// A text file of the site at `base`, hop by hop: its URLs come from the site, so each is held
    /// to the public check, robots.txt and that site. `None` for anything else.
    async fn read_text(&self, url: &str, base: &str) -> Option<String> {
        let site = |url: &Url| url.host_str().map(|host| registrable(host).to_owned());
        let wanted = site(&Url::parse(base).ok()?)?;
        let mut url = url.to_owned();
        for _ in 0..=MAX_REDIRECTS {
            let parsed = Url::parse(&url).ok()?;
            let host = parsed.host_str()?;
            let same_site = registrable(host) == wanted
                || normalize(host).is_some_and(|host| registrable(&host) == wanted);
            if !same_site || !self.web.allowed(&url).await {
                return None;
            }
            let _slot = self.web.slot(host).await;
            let request = self
                .web
                .single()
                .get(parsed.clone())
                .header(header::USER_AGENT, self.web.user_agent())
                // Compressed, a small read could decode to far more than the limit.
                .header(header::ACCEPT_ENCODING, "identity")
                .send();
            let mut response = timeout(REQUEST_TIMEOUT, request).await.ok()?.ok()?;
            if response.status().is_redirection() {
                let location = response.headers().get(header::LOCATION)?.to_str().ok()?;
                url = parsed.join(location).ok()?.into();
                continue;
            }
            let encoded = response.headers().get(header::CONTENT_ENCODING);
            if !response.status().is_success()
                || encoded.is_some_and(|encoding| encoding != "identity")
            {
                return None;
            }
            let mut body = Vec::new();
            while let Some(chunk) = timeout(REQUEST_TIMEOUT, response.chunk()).await.ok()?.ok()? {
                body.extend_from_slice(&chunk);
                // What fits still counts: a long sitemap is cut, not refused.
                if body.len() > MAX_FILE_BYTES {
                    break;
                }
            }
            return Some(String::from_utf8_lossy(&body).into_owned());
        }
        None
    }
}
