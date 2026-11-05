//! Stand-ins for the research steps' services, as `tests/fakes.py` had them.

#![allow(dead_code)]

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use kvasir_config::Model;
use kvasir_fetch::{Engine, FetchError, Page, PageStore};
use kvasir_llm::{BoxFuture, Target};
use kvasir_research::claims::{Claim, Confidence};
use kvasir_research::{Classify, Encoder, Hit, Pages, Search, Tools};
use kvasir_run::{Run, RunCtx};
use kvasir_testkit::ScriptedLlm;
use kvasir_types::runs::Progress;
use kvasir_types::sites::Tier;

pub fn model() -> Model {
    Model { name: "m".into(), api_base: "http://unused".into(), api_key: "none".into() }
}

pub fn target(role: &'static str) -> Target {
    Target {
        model: model(),
        role,
        max_tokens: 500,
        temperature: 0.0,
        top_p: None,
        reasoning_effort: None,
    }
}

/// Tools calling `llm` for each of `roles`, as wide as one call, with no search, no pages and no
/// source classifier unless set after.
pub fn tools(llm: &Arc<ScriptedLlm>, roles: &[&'static str]) -> Tools {
    let llm = Arc::clone(llm);
    Tools {
        llm: llm.clone(),
        targets: roles.iter().map(|&role| (role, target(role))).collect(),
        encoder: Encoder { llm, model: model() },
        search: Arc::new(Results::new(Vec::new(), Vec::new())),
        pages: Arc::new(Fetched::default()),
        sources: None,
        context_tokens: 16384,
        max_threads: 1,
        max_age: None,
    }
}

/// The roles called, in order.
pub fn calls(llm: &ScriptedLlm) -> Vec<&'static str> {
    llm.sent().into_iter().map(|sent| sent.role).collect()
}

pub fn ctx() -> RunCtx {
    RunCtx::new(Arc::new(Run::new("ledger", "topic", Vec::new(), false)))
}

/// The events `ctx`'s run recorded, in order.
pub fn events(ctx: &RunCtx) -> Vec<Progress> {
    ctx.run.detail().events
}

pub fn claim(id: u64, url: &str) -> Claim {
    quoting(id, url, &format!("quote #{id}."))
}

pub fn quoting(id: u64, url: &str, quote: &str) -> Claim {
    Claim {
        id,
        text: format!("claim {id}"),
        quote: quote.into(),
        url: url.into(),
        title: url.into(),
        confidence: Confidence::High,
        step: 1,
        from_snippet: false,
        corroborated_by: Vec::new(),
        contradicted_by: Vec::new(),
    }
}

/// A fetched page holding `text`, with no links.
pub fn page(text: &str) -> Page {
    Page {
        url: "https://a".into(),
        final_url: "https://a".into(),
        title: String::new(),
        text: text.into(),
        engine: Engine::Static,
        links: Vec::new(),
        fetched_at: 0.0,
    }
}

pub fn at(url: &str, text: &str) -> Page {
    Page { url: url.into(), final_url: url.into(), ..page(text) }
}

/// SearXNG, answering every query with the same results, or failing as told, and recording the
/// queries.
pub struct Results {
    hits: Vec<Hit>,
    failures: Vec<String>,
    pub queries: Mutex<Vec<String>>,
}

impl Results {
    pub fn new(hits: Vec<Hit>, failures: Vec<String>) -> Self {
        Self { hits, failures, queries: Mutex::new(Vec::new()) }
    }
}

impl Search for Results {
    fn search<'a>(
        &'a self,
        query: &'a str,
        _exclude: &'a HashSet<String>,
        _ctx: &'a RunCtx,
    ) -> BoxFuture<'a, (Vec<Hit>, Vec<String>)> {
        self.queries.lock().unwrap().push(query.to_owned());
        let hits = if self.failures.is_empty() { self.hits.clone() } else { Vec::new() };
        Box::pin(async move { (hits, self.failures.clone()) })
    }
}

/// Pages by URL; any other fails to fetch. Every lead is allowed.
#[derive(Default)]
pub struct Fetched {
    pub pages: HashMap<String, Page>,
    /// Served for any URL not in `pages`.
    pub any: Option<Page>,
}

impl Pages for Fetched {
    fn fetch<'a>(
        &'a self,
        url: &'a str,
        _max_age: Option<u32>,
    ) -> BoxFuture<'a, Result<Page, FetchError>> {
        let found = self.pages.get(url).or(self.any.as_ref()).cloned();
        Box::pin(async move { found.ok_or_else(|| FetchError::Failed("down".into())) })
    }

    fn allowed<'a>(&'a self, _url: &'a str) -> BoxFuture<'a, bool> {
        Box::pin(async { true })
    }

    fn cache(&self) -> Option<PageStore> {
        None
    }
}

/// Classifies each host as the tier it was told and any other as `unknown`, recording the hosts
/// of each call.
#[derive(Default)]
pub struct Tiers {
    tiers: HashMap<String, Tier>,
    pub calls: Mutex<Vec<HashSet<String>>>,
}

impl Tiers {
    pub fn new(tiers: &[(&str, Tier)]) -> Self {
        let tiers = tiers.iter().map(|(host, tier)| ((*host).to_owned(), *tier)).collect();
        Self { tiers, calls: Mutex::default() }
    }
}

impl Classify for Tiers {
    fn tiers<'a>(
        &'a self,
        hosts: Vec<String>,
        _deadline: Duration,
    ) -> BoxFuture<'a, HashMap<String, Tier>> {
        self.calls.lock().unwrap().push(hosts.iter().cloned().collect());
        let found = hosts
            .into_iter()
            .map(|host| {
                let tier = self.tiers.get(&host).copied().unwrap_or(Tier::Unknown);
                (host, tier)
            })
            .collect();
        Box::pin(async move { found })
    }
}
