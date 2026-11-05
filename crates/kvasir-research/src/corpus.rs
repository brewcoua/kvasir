//! What a run has read: every piece of every page, with its vector, and which pieces intake has
//! read, so that a later question can be answered from pages already downloaded. Also the links of
//! those pages, kept as leads for a search to follow when one looks worth more than a result.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use indexmap::IndexMap;
use kvasir_extract::{Region, normalize};
use kvasir_fetch::{Page, PageStore, domain};
use kvasir_llm::{LlmError, Sink};
use kvasir_types::sites::Tier;
use regex::Regex;

use crate::Classify;
use crate::claims::Claim;
use crate::encoder::{Encoder, cosine};
use crate::search::{Hit, NOVELTY_WEIGHT, descending};

// Untuned first guesses, like the weights of search.rs. The cosine a piece must reach with the
// question to be recalled, and how many pieces one recall returns, at most this many from one
// site, so that a single site cannot fill it.
pub const RECALL_MIN: f64 = 0.6;
pub const RECALL_PIECES: usize = 3;
pub const RECALL_PER_SITE: usize = 2;
// Also untuned. A page leaves at most LEADS_PER_PAGE links behind, none of a page more than
// LEAD_DEPTH links from a search result. A lead is a candidate only above LEAD_MIN cosine to the
// question, so that a thin search does not get a poor lead read in its place. A lead's worth is its
// relevance, plus NOVELTY_WEIGHT (search.rs's) times its novelty, plus AFFINITY_WEIGHT times its
// site's affinity, plus RELATED_BONUS for a link in a related block. A site whose affinity reaches
// SITE_AFFINITY earns a search of its own, SITE_SEARCHES of them at most in a run, if its tier is
// high or normal. The trust bonus of a tier is added to a lead's worth, and each corroboration
// counts in affinity by the corroboration weight of the corroborating claim's tier (sources
// enabled only). No run reads more than PAGES_PER_SITE pages of one site.
const LEADS_PER_PAGE: usize = 20;
const LEAD_DEPTH: u32 = 2;
const LEAD_MIN: f64 = 0.5;
const AFFINITY_WEIGHT: f64 = 0.5;
const RELATED_BONUS: f64 = 0.1;
const SITE_AFFINITY: f64 = 1.0;
pub const PAGES_PER_SITE: usize = 6;
const SITE_SEARCHES: usize = 2;
/// The pieces one search of the store returns at most.
const STORED_PIECES: usize = 200;

fn trust_bonus(tier: Tier) -> f64 {
    match tier {
        Tier::High => 0.1,
        Tier::Normal => 0.05,
        Tier::Unknown | Tier::Avoid => 0.0,
        Tier::Low => -0.1,
    }
}

fn corroboration_weight(tier: Tier) -> f64 {
    match tier {
        Tier::High | Tier::Normal => 1.0,
        Tier::Unknown => 0.5,
        Tier::Low => 0.25,
        Tier::Avoid => 0.0,
    }
}

static NON_WORD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[\W_]+").expect("a valid pattern"));

/// A piece of a page, with its vector, and whether intake has read it.
#[derive(Clone, Debug)]
pub struct Piece {
    pub page: Arc<Page>,
    pub text: String,
    pub vector: Vec<f32>,
    pub read: bool,
}

/// A link of a page read, to a page that is not: what it says about itself and its vector.
#[derive(Clone, Debug)]
struct Lead {
    url: String,
    anchor: String,
    context: String,
    region: Region,
    /// Links from a search result: 1 for a link of a result, 2 for one of that page, and so on.
    depth: u32,
    vector: Vec<f32>,
}

/// The pieces of the pages a run read, and how well each site has done. With a `store`, the pieces
/// are kept there, as vectors of `model`, and recall reaches pieces other runs kept, unless
/// `recall_past_runs` is off. With `sources`, sites of tier `avoid` are never recalled or led to,
/// and the tier weighs in a lead's worth, a site's affinity and its site search.
#[derive(Default)]
pub struct Corpus {
    store: Option<PageStore>,
    model: String,
    recall_past_runs: bool,
    pub sources: Option<Arc<dyn Classify>>,
    pub pieces: Vec<Piece>,
    /// Pages whose every piece is here, none of which the store can add.
    whole: BTreeSet<String>,
    /// Pages read, by site.
    pub pages: HashMap<String, usize>,
    /// Claims kept, by site.
    pub claims: HashMap<String, usize>,
    // By normalized URL, so that two spellings of a page are one lead.
    leads: IndexMap<String, Lead>,
    refused: HashSet<String>,
    site_searched: HashSet<(u64, String)>,
    site_searches: usize,
    // Sites found `avoid`, kept so that a search of the store leaves them out the first time.
    avoid: BTreeSet<String>,
}

impl Corpus {
    pub fn new(
        store: Option<PageStore>,
        model: String,
        recall_past_runs: bool,
        sources: Option<Arc<dyn Classify>>,
    ) -> Self {
        Self { store, model, recall_past_runs, sources, ..Self::default() }
    }

    fn page_count(&self, site: &str) -> usize {
        self.pages.get(site).copied().unwrap_or(0)
    }

    /// Adds the pieces of `page`, none read yet, and returns their indices in `pieces`.
    pub async fn add(
        &mut self,
        page: Arc<Page>,
        texts: Vec<String>,
        vectors: Vec<Vec<f32>>,
    ) -> std::ops::Range<usize> {
        *self.pages.entry(domain(&page.url)).or_default() += 1;
        self.whole.insert(page.url.clone());
        if let Some(store) = &self.store {
            let pieces = texts.iter().cloned().zip(vectors.iter().cloned()).collect();
            // The cache saves reading; one that fails must not cost the run.
            if let Err(err) = store.put_pieces(&page.url, pieces, &self.model).await {
                tracing::warn!("pieces of {} not cached: {err}", page.url);
            }
        }
        let start = self.pieces.len();
        for (text, vector) in texts.into_iter().zip(vectors) {
            self.pieces.push(Piece { page: Arc::clone(&page), text, vector, read: false });
        }
        start..self.pieces.len()
    }

    /// Keeps the links of `page` in a related block or its text as leads of `depth`, related first
    /// and then in page order, and not those past `LEAD_DEPTH`. A lead already kept, or refused,
    /// stays as it was.
    pub async fn add_leads(
        &mut self,
        encoder: &Encoder,
        page: &Page,
        depth: u32,
        sink: &dyn Sink,
    ) -> Result<(), LlmError> {
        if depth > LEAD_DEPTH {
            return Ok(());
        }
        let own: Vec<String> =
            [&page.url, &page.final_url].into_iter().filter_map(|u| normalize(u).ok()).collect();
        let mut links: Vec<(String, &kvasir_extract::Link)> = page
            .links
            .iter()
            .filter(|link| matches!(link.region, Region::Related | Region::Main))
            .filter_map(|link| Some((normalize(&link.url).ok()?, link)))
            .filter(|(key, _)| {
                !own.contains(key) && !self.leads.contains_key(key) && !self.refused.contains(key)
            })
            .collect();
        // A stable sort, so each region keeps the page's order.
        links.sort_by_key(|(_, link)| link.region != Region::Related);
        links.truncate(LEADS_PER_PAGE);
        if links.is_empty() {
            return Ok(());
        }
        let texts: Vec<String> = links
            .iter()
            .map(|(_, link)| {
                let path =
                    url::Url::parse(&link.url).map(|u| u.path().to_owned()).unwrap_or_default();
                let words = NON_WORD.replace_all(&path, " ");
                format!("{}\n{}\n{}", link.anchor, link.context, words.trim())
            })
            .collect();
        let vectors = encoder.encode(&texts, false, sink).await?;
        for ((key, link), vector) in links.into_iter().zip(vectors) {
            let lead = Lead {
                url: link.url.clone(),
                anchor: link.anchor.clone(),
                context: link.context.clone(),
                region: link.region,
                depth,
                vector,
            };
            self.leads.insert(key, lead);
        }
        Ok(())
    }

    /// Drops the lead at `url` for good, as one its site's robots.txt does not allow.
    pub fn refuse(&mut self, url: &str) {
        if let Ok(key) = normalize(url) {
            self.leads.shift_remove(&key);
            self.refused.insert(key);
        }
    }

    /// The tier of each of `hosts`; all `unknown` without a classifier. With no `deadline` only
    /// what the cache, rules and lists know, as the hosts were classified when found.
    pub async fn tiers(&self, hosts: Vec<String>, deadline: Duration) -> HashMap<String, Tier> {
        match &self.sources {
            None => hosts.into_iter().map(|host| (host, Tier::Unknown)).collect(),
            Some(sources) => sources.tiers(hosts, deadline).await,
        }
    }

    async fn claim_tiers(&self, claims: &[Claim], deadline: Duration) -> HashMap<String, Tier> {
        let hosts: BTreeSet<String> = claims.iter().map(|claim| domain(&claim.url)).collect();
        self.tiers(hosts.into_iter().collect(), deadline).await
    }

    /// The sites of `claims` that are now of tier `avoid`.
    pub async fn avoided(&self, claims: &[Claim], deadline: Duration) -> BTreeSet<String> {
        let tiers = self.claim_tiers(claims, deadline).await;
        tiers.into_iter().filter(|(_, tier)| *tier == Tier::Avoid).map(|(host, _)| host).collect()
    }

    /// The ids of the `claims` read from sites of tier `low`.
    pub async fn low_trust(&self, claims: &[Claim]) -> HashSet<u64> {
        let tiers = self.claim_tiers(claims, Duration::ZERO).await;
        claims
            .iter()
            .filter(|claim| tiers.get(&domain(&claim.url)) == Some(&Tier::Low))
            .map(|claim| claim.id)
            .collect()
    }

    /// Whether the run has read as many pages as it may from the site of `url`.
    pub fn full(&self, url: &str) -> bool {
        self.page_count(&domain(url)) >= PAGES_PER_SITE
    }

    /// The `limit` leads worth most for `question`, best first, as search hits. Those under
    /// `LEAD_MIN`, already seen or read, of an `avoid` site, or of a site read `PAGES_PER_SITE`
    /// times are left out.
    #[allow(clippy::too_many_arguments)]
    pub async fn leads_for(
        &self,
        encoder: &Encoder,
        question: &str,
        held: Option<&[Vec<f32>]>,
        claims: &[Claim],
        seen: &HashSet<String>,
        limit: usize,
        sink: &dyn Sink,
    ) -> Result<Vec<Hit>, LlmError> {
        let skip: HashSet<String> =
            seen.iter().chain(&self.whole).filter_map(|url| normalize(url).ok()).collect();
        let candidates: Vec<&Lead> = self
            .leads
            .iter()
            .filter(|(key, lead)| !skip.contains(*key) && !self.full(&lead.url))
            .map(|(_, lead)| lead)
            .collect();
        let hosts: BTreeSet<String> = candidates.iter().map(|lead| domain(&lead.url)).collect();
        let tiers = self.tiers(hosts.into_iter().collect(), Duration::ZERO).await;
        let tier = |url: &str| tiers.get(&domain(url)).copied().unwrap_or(Tier::Unknown);
        let candidates: Vec<&Lead> =
            candidates.into_iter().filter(|lead| tier(&lead.url) != Tier::Avoid).collect();
        if candidates.is_empty() {
            return Ok(Vec::new());
        }
        let wanted = encoder.one(question, true, sink).await?;
        let affinity = self.affinity(claims).await;
        let mut order = Vec::new();
        let mut worth = Vec::new();
        for (i, lead) in candidates.iter().enumerate() {
            let relevance = cosine(&wanted, &lead.vector);
            let novelty = match held.filter(|held| !held.is_empty()) {
                Some(held) => {
                    1.0 - held.iter().map(|h| cosine(&lead.vector, h)).fold(f64::MIN, f64::max)
                }
                None => 0.0,
            };
            worth.push(
                relevance
                    + NOVELTY_WEIGHT * novelty
                    + AFFINITY_WEIGHT * affinity.get(&domain(&lead.url)).copied().unwrap_or(0.0)
                    + if lead.region == Region::Related { RELATED_BONUS } else { 0.0 }
                    + trust_bonus(tier(&lead.url)),
            );
            if relevance >= LEAD_MIN {
                order.push(i);
            }
        }
        order.sort_by(|&a, &b| worth[b].total_cmp(&worth[a]));
        Ok(order
            .into_iter()
            .take(limit)
            .map(|i| Hit {
                url: candidates[i].url.clone(),
                title: candidates[i].anchor.clone(),
                description: candidates[i].context.clone(),
                depth: Some(candidates[i].depth),
                ..Hit::default()
            })
            .collect())
    }

    /// A search of the site with the highest affinity, `SITE_AFFINITY` or more, that this question
    /// has not searched and that is under `PAGES_PER_SITE`. With `sources`, only a site of tier
    /// `high` or `normal`. None when no site is one, or after `SITE_SEARCHES` in the run.
    pub async fn site_query(
        &mut self,
        question_id: u64,
        question: &str,
        claims: &[Claim],
    ) -> Option<String> {
        if self.site_searches >= SITE_SEARCHES {
            return None;
        }
        let affinity = self.affinity(claims).await;
        let mut sites: Vec<(&String, f64)> = affinity.iter().map(|(s, a)| (s, *a)).collect();
        sites.sort_by(|a, b| b.1.total_cmp(&a.1));
        for (site, affinity) in sites {
            if affinity < SITE_AFFINITY {
                break;
            }
            let trusted = match &self.sources {
                None => true,
                Some(_) => {
                    let tiers = self.tiers(vec![site.clone()], Duration::ZERO).await;
                    matches!(tiers.get(site), Some(Tier::High | Tier::Normal))
                }
            };
            let searched = self.site_searched.contains(&(question_id, site.clone()));
            if self.page_count(site) < PAGES_PER_SITE && !searched && trusted {
                self.site_searched.insert((question_id, site.clone()));
                self.site_searches += 1;
                return Some(format!("site:{site} {question}"));
            }
        }
        None
    }

    /// Per site, its claims another site corroborates over its pages read. With `sources`, a claim
    /// counts by the best corroboration weight among its corroborating claims' tiers.
    /// Corroboration is known only to a frontier that merges claims, so under the breadth frontier
    /// this is empty and a lead's worth is its relevance, novelty and region.
    async fn affinity(&self, claims: &[Claim]) -> IndexMap<String, f64> {
        let by_id: HashMap<u64, &Claim> = claims.iter().map(|claim| (claim.id, claim)).collect();
        let tiers = self.claim_tiers(claims, Duration::ZERO).await;
        let mut confirmed: IndexMap<String, f64> = IndexMap::new();
        for claim in claims {
            let weight = if self.sources.is_none() {
                if claim.corroborated_by.is_empty() { 0.0 } else { 1.0 }
            } else {
                claim
                    .corroborated_by
                    .iter()
                    .filter_map(|i| by_id.get(i))
                    .map(|other| {
                        corroboration_weight(
                            tiers.get(&domain(&other.url)).copied().unwrap_or(Tier::Unknown),
                        )
                    })
                    .fold(0.0, f64::max)
            };
            *confirmed.entry(domain(&claim.url)).or_default() += weight;
        }
        confirmed
            .into_iter()
            .filter(|(site, n)| *n != 0.0 && self.page_count(site) > 0)
            .map(|(site, n)| {
                let pages = self.page_count(&site) as f64;
                (site, n / pages)
            })
            .collect()
    }

    /// Counts `claims` kept against the sites they were read from.
    pub fn record(&mut self, claims: &[Claim]) {
        for claim in claims {
            *self.claims.entry(domain(&claim.url)).or_default() += 1;
        }
    }

    /// The unread pieces closest to `question`, best first, and marks them read. Pieces of cached
    /// pages not in the corpus compete too, and join it when recalled. Pieces of `avoid` sites are
    /// never recalled.
    pub async fn recall(
        &mut self,
        encoder: &Encoder,
        question: &str,
        sink: &dyn Sink,
    ) -> Result<Vec<(Arc<Page>, String)>, LlmError> {
        let hosts: BTreeSet<String> = self.pieces.iter().map(|p| domain(&p.page.url)).collect();
        let tiers = self.tiers(hosts.into_iter().collect(), Duration::ZERO).await;
        // Held pieces by index, then the stored ones after them.
        let held: Vec<usize> = (0..self.pieces.len())
            .filter(|&i| {
                let piece = &self.pieces[i];
                !piece.read && tiers.get(&domain(&piece.page.url)) != Some(&Tier::Avoid)
            })
            .collect();
        let stored = self.stored(question).await;
        if held.is_empty() && stored.is_empty() {
            return Ok(Vec::new());
        }
        let candidate =
            |n: usize| if n < held.len() { &self.pieces[held[n]] } else { &stored[n - held.len()] };
        let wanted = encoder.one(question, true, sink).await?;
        let scores: Vec<f64> =
            (0..held.len() + stored.len()).map(|n| cosine(&wanted, &candidate(n).vector)).collect();
        let mut recalled = Vec::new();
        let mut sites: HashMap<String, usize> = HashMap::new();
        for n in descending(&scores) {
            if scores[n] < RECALL_MIN || recalled.len() == RECALL_PIECES {
                break;
            }
            let count = sites.entry(domain(&candidate(n).page.url)).or_default();
            if *count < RECALL_PER_SITE {
                recalled.push(n);
                *count += 1;
            }
        }
        let mut out = Vec::new();
        for n in recalled {
            if n < held.len() {
                let piece = &mut self.pieces[held[n]];
                piece.read = true;
                out.push((Arc::clone(&piece.page), piece.text.clone()));
                continue;
            }
            let mut piece = stored[n - held.len()].clone();
            piece.read = true;
            if self.pieces.iter().all(|other| other.page.url != piece.page.url) {
                *self.pages.entry(domain(&piece.page.url)).or_default() += 1;
            }
            out.push((Arc::clone(&piece.page), piece.text.clone()));
            self.pieces.push(piece);
        }
        Ok(out)
    }

    /// The cached pieces that best match the words of `question`, less those held here. A page
    /// recalled from the store is held only in part: its others may answer a later question.
    async fn stored(&mut self, question: &str) -> Vec<Piece> {
        let Some(store) = self.store.clone().filter(|_| self.recall_past_runs) else {
            return Vec::new();
        };
        let whole: Vec<String> = self.whole.iter().cloned().collect();
        let model = self.model.clone();
        let search = |avoid: BTreeSet<String>| {
            let (store, whole, model) = (store.clone(), whole.clone(), model.clone());
            async move { store.search(question, &model, &whole, STORED_PIECES, &avoid).await }
        };
        let mut found = match search(self.avoid.clone()).await {
            Ok(found) => found,
            Err(err) => {
                tracing::warn!("cached pieces not searched: {err}");
                return Vec::new();
            }
        };
        let hosts: BTreeSet<String> = found.iter().map(|f| domain(&f.page.url)).collect();
        let tiers = self.tiers(hosts.into_iter().collect(), Duration::ZERO).await;
        let new: Vec<String> =
            tiers.into_iter().filter(|(_, t)| *t == Tier::Avoid).map(|(h, _)| h).collect();
        if !new.is_empty() {
            self.avoid.extend(new);
            found = match search(self.avoid.clone()).await {
                Ok(found) => found,
                Err(err) => {
                    tracing::warn!("cached pieces not searched: {err}");
                    return Vec::new();
                }
            };
        }
        let held: HashSet<(&str, &str)> =
            self.pieces.iter().map(|p| (p.page.url.as_str(), p.text.as_str())).collect();
        found
            .into_iter()
            .filter(|f| !held.contains(&(f.page.url.as_str(), f.text.as_str())))
            .map(|f| Piece { page: Arc::new(f.page), text: f.text, vector: f.vector, read: false })
            .collect()
    }
}
