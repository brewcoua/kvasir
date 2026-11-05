//! Searching, skipping what the run already read, and choosing which results to read.
//!
//! Each query's results are ranked four ways before any page is downloaded: by SearXNG's score, by
//! closeness of the title and snippet to the question, by distance from what the run holds, and by
//! the trust tier of the site. A result of an `avoid` site is dropped before ranking. The rankings
//! are fused with weighted reciprocal rank fusion (Cormack, Clarke and Buttcher, SIGIR 2009), then
//! picked greedily, skipping a repeated domain or a near-duplicate snippet, as in maximal marginal
//! relevance (Carbonell and Goldstein, 1998). Without an encoder only SearXNG's score and the domain
//! skip apply; without a source classifier there is no tier ranking and no drop.
//!
//! A run's leads, the links of pages it read, join each query's results as candidates, at most
//! [`LEADS_PER_QUERY`] of them picked. Their worth stands in for SearXNG's score, which a lead has
//! not. A lead is picked only if its site's robots.txt allows it; a result is fetched as it is. No
//! hit is picked from a site the run has read its limit of pages from.
//!
//! The weights are not tuned. They are a first guess, to be kept or changed on evaluation.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use kvasir_fetch::{Web, domain};
use kvasir_llm::BoxFuture;
use kvasir_run::RunCtx;
use kvasir_sites::sources::DEADLINE;
use kvasir_types::runs::SearchResult;
use kvasir_types::sites::Tier;
use regex::Regex;
use serde::Deserialize;

use crate::claims::Claim;
use crate::corpus::Corpus;
use crate::encoder::{Encoder, cosine};
use crate::{ResearchError, Tools};

/// Results of one query that are ranked; the rest are dropped.
pub const POOL: usize = 20;
/// Engines read the first few dozen words of a query and ignore the rest, so a longer one only adds
/// noise; a planner that degenerated once wrote thousands of characters of quoted terms.
pub const MAX_QUERY_CHARACTERS: usize = 300;
/// Reciprocal rank fusion's constant, 60 in the paper: how flat the reward for a high rank is.
const RRF_K: f64 = 60.0;
// What each ranking adds to the fused score.
const SEARXNG_WEIGHT: f64 = 1.0;
const RELEVANCE_WEIGHT: f64 = 1.0;
pub const NOVELTY_WEIGHT: f64 = 0.5;
const TRUST_WEIGHT: f64 = 0.5;
/// The tiers in the order they rank, best first; `avoid` never reaches the ranking.
const TRUST_ORDER: [Tier; 4] = [Tier::High, Tier::Normal, Tier::Unknown, Tier::Low];
/// Above this cosine similarity, a snippet repeats one already picked for the query.
const NEAR_DUPLICATE: f64 = 0.9;
/// Leads picked for one query, however many are worth reading.
pub const LEADS_PER_QUERY: usize = 2;
/// How long SearXNG has to answer.
const TIMEOUT: Duration = Duration::from_secs(30);

// What a cut query must not end on: a boolean operator, or a prefix operator whose term was cut.
static DANGLING: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\s*(?:\b(?:OR|AND|NOT)|[-+!|])\s*$").expect("a valid pattern"));

/// A search result, or a lead standing in for one.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Hit {
    pub url: String,
    pub title: String,
    pub description: String,
    pub snippets: Vec<String>,
    pub score: f64,
    /// For a lead, how many links it is from a search result.
    pub depth: Option<u32>,
}

/// A search engine: SearXNG, or a stand-in in tests.
pub trait Search: Send + Sync {
    /// What `query` found, less `exclude`, and why it failed if it did. Reports the query to `ctx`.
    fn search<'a>(
        &'a self,
        query: &'a str,
        exclude: &'a HashSet<String>,
        ctx: &'a RunCtx,
    ) -> BoxFuture<'a, (Vec<Hit>, Vec<String>)>;
}

/// A SearXNG instance serving its JSON format. One serving HTML only returns an empty result set
/// rather than failing, which looks like a topic with no sources.
pub struct SearXng {
    url: String,
    client: reqwest::Client,
    web: Arc<Web>,
}

impl SearXng {
    /// The instance is the operator's own and may be on their network, so its address is not
    /// guarded. Its requests take one of `web`'s process permits.
    pub fn new(url: &str, web: Arc<Web>) -> Self {
        // SearXNG answers its base URL with a 308 to /search, so redirects are followed.
        let client =
            reqwest::Client::builder().timeout(TIMEOUT).build().expect("the TLS backend starts");
        Self { url: url.to_owned(), client, web }
    }

    async fn rows(&self, query: &str) -> Result<Answer, String> {
        let mut url = url::Url::parse(&self.url).map_err(|e| format!("search.url: {e}"))?;
        url.query_pairs_mut().append_pair("q", query).append_pair("format", "json");
        let _permit = self.web.permit().await;
        let response = self.client.get(url).send().await.map_err(|e| format!("request: {e}"))?;
        response.json::<Answer>().await.map_err(|e| format!("answer: {e}"))
    }
}

#[derive(Deserialize)]
struct Answer {
    results: Vec<Row>,
    #[serde(default)]
    unresponsive_engines: Vec<(String, String)>,
}

#[derive(Deserialize)]
struct Row {
    url: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    content: String,
    #[serde(default)]
    score: f64,
}

impl Search for SearXng {
    fn search<'a>(
        &'a self,
        query: &'a str,
        exclude: &'a HashSet<String>,
        ctx: &'a RunCtx,
    ) -> BoxFuture<'a, (Vec<Hit>, Vec<String>)> {
        Box::pin(async move {
            let search = ctx.search_started("SearXNG", query);
            let (rows, failures) = match self.rows(query).await {
                // Rate limits and CAPTCHAs leave the engine out and the answer empty.
                Ok(answer) if answer.results.is_empty() => {
                    let failures = answer
                        .unresponsive_engines
                        .iter()
                        .map(|(engine, reason)| format!("{engine}: {reason}"))
                        .collect();
                    (Vec::new(), failures)
                }
                Ok(answer) => (answer.results, Vec::new()),
                Err(err) => {
                    tracing::error!("Error occurs when searching query {query}: {err}");
                    (Vec::new(), vec![err])
                }
            };
            let reported = rows
                .iter()
                .map(|row| SearchResult {
                    title: row.title.clone(),
                    url: row.url.clone(),
                    snippet: row.content.clone(),
                    source: None,
                })
                .collect();
            ctx.search_ended(search, reported, failures.clone());
            let hits = rows
                .into_iter()
                .filter(|row| !exclude.contains(&row.url))
                .map(|row| Hit {
                    snippets: vec![row.content.clone()],
                    description: row.content,
                    title: row.title,
                    url: row.url,
                    score: row.score,
                    depth: None,
                })
                .collect();
            (hits, failures)
        })
    }
}

/// `query` cut to [`MAX_QUERY_CHARACTERS`] at a word boundary, without a half-quoted term or an
/// operator left hanging by the cut.
pub fn cap(query: &str) -> String {
    let chars: Vec<char> = query.chars().collect();
    if chars.len() <= MAX_QUERY_CHARACTERS {
        return query.to_owned();
    }
    let mut cut: String = chars[..MAX_QUERY_CHARACTERS].iter().collect();
    if let Some(space) = cut.rfind(' ').filter(|&space| space > 0)
        && !chars[MAX_QUERY_CHARACTERS].is_whitespace()
    {
        cut.truncate(space);
    }
    if cut.matches('"').count() % 2 == 1 {
        cut.truncate(cut.rfind('"').expect("a quote"));
    }
    cut.truncate(cut.trim_end().len());
    while let Some(found) = DANGLING.find(&cut) {
        cut.truncate(found.start());
    }
    tracing::info!("search query cut from {} to {} characters", chars.len(), cut.chars().count());
    cut
}

/// What a search ranks its results by, besides SearXNG's score.
#[derive(Default)]
pub struct Ranking<'a> {
    pub encoder: Option<&'a Encoder>,
    /// What the results are for.
    pub question: &'a str,
    /// The vectors of what the run holds, which a result is better for being far from.
    pub held: Option<&'a [Vec<f32>]>,
    /// With an encoder, its leads for `question` join the results.
    pub corpus: Option<&'a mut Corpus>,
    /// What the leads' site affinity is counted from.
    pub claims: &'a [Claim],
}

/// Results not read before, at most `per_query` from each query. Marks them seen.
///
/// The results are the best by SearXNG's score, fused with the rankings of `ranking`. At most one
/// result per domain is taken, and none that repeats another's snippet; the next ranked takes a
/// skipped one's place. Skipped results are not marked seen.
///
/// With a source classifier, the hosts of all the queries' results and leads are classified in one
/// batch, results of an `avoid` site are dropped, and marked seen, even when nothing else is left,
/// and the tier is a fourth ranking when the pool's tiers differ.
pub async fn search(
    tools: &Tools,
    ctx: &RunCtx,
    queries: &[String],
    per_query: usize,
    seen: &mut HashSet<String>,
    mut ranking: Ranking<'_>,
) -> Result<Vec<Hit>, ResearchError> {
    let mut failures = Vec::new();
    let mut gathered = Vec::new();
    for query in queries {
        ctx.run.record_search(1);
        let (found, failed) = tools.search.search(&cap(query), seen, ctx).await;
        failures.extend(failed);
        let mut results: Vec<Hit> =
            found.into_iter().filter(|hit| !seen.contains(&hit.url)).collect();
        results.sort_by(|a, b| b.score.total_cmp(&a.score));
        results.truncate(POOL);
        let leads = match (ranking.corpus.as_deref(), ranking.encoder) {
            (Some(corpus), Some(encoder)) => {
                let held = ranking.held;
                corpus
                    .leads_for(encoder, ranking.question, held, ranking.claims, seen, POOL, ctx)
                    .await?
            }
            _ => Vec::new(),
        };
        gathered.push((results, leads));
    }
    let mut tiers = HashMap::new();
    if let Some(sources) = &tools.sources {
        let mut hosts: Vec<String> = Vec::new();
        for hit in gathered.iter().flat_map(|(results, leads)| results.iter().chain(leads)) {
            let host = domain(&hit.url);
            if !hosts.contains(&host) {
                hosts.push(host);
            }
        }
        tiers = sources.tiers(hosts, DEADLINE).await;
        for hit in gathered.iter().flat_map(|(results, leads)| results.iter().chain(leads)) {
            if tiers.get(&domain(&hit.url)) == Some(&Tier::Avoid) {
                seen.insert(hit.url.clone());
            }
        }
    }
    let mut hits = Vec::new();
    for (results, leads) in gathered {
        // An earlier query may have picked a URL, or it may be an `avoid` one.
        let results: Vec<Hit> =
            results.into_iter().filter(|hit| !seen.contains(&hit.url)).collect();
        let leads: Vec<Hit> = leads.into_iter().filter(|hit| !seen.contains(&hit.url)).collect();
        let first_lead = results.len();
        // Results by SearXNG's score, leads by their worth.
        let mut fused: Vec<f64> = (1..=results.len())
            .chain(1..=leads.len())
            .map(|rank| SEARXNG_WEIGHT / (RRF_K + rank as f64))
            .collect();
        let pool: Vec<Hit> = results.into_iter().chain(leads).collect();
        if pool.is_empty() {
            continue;
        }
        let mut vectors = None;
        let mut rankings: Vec<(f64, Vec<f64>)> = Vec::new();
        if let Some(encoder) = ranking.encoder {
            let texts: Vec<String> =
                pool.iter().map(|hit| format!("{}\n{}", hit.title, hit.description)).collect();
            let encoded = encoder.encode(&texts, false, ctx).await?;
            let wanted = encoder.one(ranking.question, true, ctx).await?;
            rankings.push((RELEVANCE_WEIGHT, encoded.iter().map(|v| cosine(&wanted, v)).collect()));
            if let Some(held) = ranking.held.filter(|held| !held.is_empty()) {
                let novelty = encoded
                    .iter()
                    .map(|v| 1.0 - held.iter().map(|h| cosine(v, h)).fold(f64::MIN, f64::max))
                    .collect();
                rankings.push((NOVELTY_WEIGHT, novelty));
            }
            vectors = Some(encoded);
        }
        if !tiers.is_empty() {
            let trust: Vec<f64> = pool
                .iter()
                .map(|hit| {
                    let tier = tiers.get(&domain(&hit.url)).copied().unwrap_or(Tier::Unknown);
                    -(TRUST_ORDER.iter().position(|t| *t == tier).unwrap_or(2) as f64)
                })
                .collect();
            if trust.iter().any(|t| *t != trust[0]) {
                rankings.push((TRUST_WEIGHT, trust));
            }
        }
        for (weight, scores) in rankings {
            for (rank, i) in descending(&scores).into_iter().enumerate() {
                fused[i] += weight / (RRF_K + (rank + 1) as f64);
            }
        }
        let mut picked: Vec<usize> = Vec::new();
        let mut domains = HashSet::new();
        let mut followed = 0;
        for i in descending(&fused) {
            if picked.len() == per_query {
                break;
            }
            let hit = &pool[i];
            let is_lead = i >= first_lead;
            let site = domain(&hit.url);
            if domains.contains(&site)
                || ranking.corpus.as_deref().is_some_and(|c| c.full(&hit.url))
            {
                continue;
            }
            if is_lead && followed == LEADS_PER_QUERY {
                continue;
            }
            if let Some(vectors) = &vectors
                && picked.iter().any(|&j| cosine(&vectors[i], &vectors[j]) > NEAR_DUPLICATE)
            {
                continue;
            }
            if is_lead
                && let Some(corpus) = ranking.corpus.as_deref_mut()
                && !tools.pages.allowed(&hit.url).await
            {
                corpus.refuse(&hit.url);
                continue;
            }
            picked.push(i);
            followed += usize::from(is_lead);
            domains.insert(site);
            seen.insert(hit.url.clone());
            hits.push(hit.clone());
        }
    }
    if hits.is_empty() && !failures.is_empty() {
        return Err(ResearchError::unavailable(queries.len(), failures));
    }
    Ok(hits)
}

/// Indices of `scores`, highest first, equal scores in their order: a stable sort.
pub(crate) fn descending(scores: &[f64]) -> Vec<usize> {
    let mut order: Vec<usize> = (0..scores.len()).collect();
    order.sort_by(|&a, &b| scores[b].total_cmp(&scores[a]));
    order
}
