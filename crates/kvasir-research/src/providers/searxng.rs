//! A SearXNG instance serving its JSON format. One serving HTML only returns an empty result set
//! rather than failing, which looks like a topic with no sources.

use std::time::Duration;

use kvasir_llm::BoxFuture;
use reqwest::StatusCode;
use reqwest::header::RETRY_AFTER;
use serde::Deserialize;

use super::{Failure, Provider, Row};

/// How long SearXNG has to answer.
const TIMEOUT: Duration = Duration::from_secs(30);

pub struct SearXng {
    url: String,
    client: reqwest::Client,
}

impl SearXng {
    /// The instance is the operator's own and may be on their network, so its address is not
    /// guarded.
    pub fn new(url: &str) -> Self {
        // SearXNG answers its base URL with a 308 to /search, so redirects are followed.
        let client =
            reqwest::Client::builder().timeout(TIMEOUT).build().expect("the TLS backend starts");
        Self { url: url.to_owned(), client }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let other =
            |what: &str, err: &dyn std::fmt::Display| Failure::Other(format!("{what}: {err}"));
        let mut url = url::Url::parse(&self.url).map_err(|e| other("search.searxng.url", &e))?;
        url.query_pairs_mut().append_pair("q", query).append_pair("format", "json");
        let response = self.client.get(url).send().await.map_err(|e| other("request", &e))?;
        if response.status() == StatusCode::TOO_MANY_REQUESTS {
            let retry_after = response
                .headers()
                .get(RETRY_AFTER)
                .and_then(|value| value.to_str().ok())
                .and_then(|text| text.parse().ok())
                .map(Duration::from_secs);
            return Err(Failure::RateLimited { retry_after });
        }
        let answer: Answer = response.json().await.map_err(|e| other("answer", &e))?;
        // Rate limits and CAPTCHAs leave the engine out and the answer empty.
        if answer.results.is_empty() && !answer.unresponsive_engines.is_empty() {
            let engines: Vec<String> = answer
                .unresponsive_engines
                .iter()
                .map(|(engine, reason)| format!("{engine}: {reason}"))
                .collect();
            return Err(Failure::Other(engines.join(", ")));
        }
        let mut results = answer.results;
        results.sort_by(|a, b| b.score.total_cmp(&a.score));
        Ok(results
            .into_iter()
            .map(|result| Row { url: result.url, title: result.title, snippet: result.content })
            .collect())
    }
}

#[derive(Deserialize)]
struct Answer {
    results: Vec<Found>,
    #[serde(default)]
    unresponsive_engines: Vec<(String, String)>,
}

#[derive(Deserialize)]
struct Found {
    url: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    content: String,
    #[serde(default)]
    score: f64,
}

impl Provider for SearXng {
    fn name(&self) -> &'static str {
        "SearXNG"
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
