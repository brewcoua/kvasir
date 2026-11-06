//! Looking hosts up on Wikidata: batched flights, backoff, the SPARQL query and its answer.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;

use kvasir_types::sites::{KINDS, registrable};
use reqwest::header;
use serde::Deserialize;
use tokio::sync::watch;
use tokio::time::Instant;

use super::{Match, Sources};

// After a transport error or a 5xx, so a down endpoint is not asked again by every search.
pub(super) const FAILURE_BACKOFF: f64 = 10.0;
pub(super) const MAX_BACKOFF: f64 = 3600.0;
pub(super) const BATCH_SIZE: usize = 50;
// WDQS answers a 429 with Retry-After; without one, this long.
pub(super) const DEFAULT_BACKOFF: f64 = 60.0;
// Past this many hosts in flight, a new one is left unknown rather than queued without end.
pub(super) const MAX_FLIGHTS: usize = 256;
pub(super) const NAMESPACE: &str = "http://www.wikidata.org/entity/";
pub(super) const ROOT_KINDS: [(&str, &str); 12] = [
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
pub(super) const INTERNATIONAL: &str = "Q484652";
pub(super) const NON_GOVERNMENTAL: &str = "Q79913";
pub(super) const ROOT_TAGS: [(&str, &str); 2] =
    [("Q171", "community"), ("Q125361038", "ai_generated")];

impl Sources {
    /// The flight of each of `keys`, starting batches for those with none.
    pub(super) async fn start(
        self: &Arc<Self>,
        keys: Vec<String>,
    ) -> HashMap<String, watch::Receiver<()>> {
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

    pub(super) fn fly(self: &Arc<Self>, batch: Vec<String>, done: watch::Sender<()>) {
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

    pub(super) async fn lookup(&self, mut hosts: Vec<String>) {
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
    pub(super) async fn ask(&self, pairs: &[(String, String)]) -> Option<HashMap<String, Match>> {
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

    pub(super) fn back_off(&self, seconds: f64) {
        let until = Instant::now() + Duration::from_secs_f64(seconds);
        *self.backoff_until.lock().unwrap_or_else(|e| e.into_inner()) = Some(until);
    }
}

pub(super) fn query(pairs: &[(String, String)]) -> String {
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
pub(super) struct Answer {
    results: Results,
}

#[derive(Deserialize)]
pub(super) struct Results {
    bindings: Vec<Binding>,
}

#[derive(Deserialize)]
pub(super) struct Binding {
    h: Value,
    i: Value,
    root: Value,
    l: Option<Value>,
}

#[derive(Deserialize)]
pub(super) struct Value {
    value: String,
}

pub(super) fn local(value: &str) -> &str {
    value.strip_prefix(NAMESPACE).unwrap_or(value)
}

/// The matches in a WDQS JSON answer, by host. A host takes the best kind across its items.
pub(super) fn parse(answer: Answer) -> HashMap<String, Match> {
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
