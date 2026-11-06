//! The Tavily search API.

use kvasir_llm::BoxFuture;
use serde::Deserialize;
use serde_json::json;

use super::{Failure, Provider, RESULTS, Row, client, other, status_failure};

const ENDPOINT: &str = "https://api.tavily.com/search";
/// Tavily's codes for a plan's limit and for a pay-as-you-go limit.
const OUT_OF_CREDITS: [u16; 2] = [432, 433];

pub struct Tavily {
    endpoint: String,
    key: String,
    client: reqwest::Client,
}

impl Tavily {
    pub fn new(key: &str) -> Self {
        Self::at(ENDPOINT, key)
    }

    /// The endpoint is fixed in code, so no setting can send the key elsewhere. Only a test picks
    /// another.
    pub fn at(endpoint: &str, key: &str) -> Self {
        Self { endpoint: endpoint.to_owned(), key: key.to_owned(), client: client() }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let response = self
            .client
            .post(&self.endpoint)
            .bearer_auth(&self.key)
            .json(&json!({"query": query, "max_results": RESULTS}))
            .send()
            .await
            .map_err(|e| other("request", e))?;
        if OUT_OF_CREDITS.contains(&response.status().as_u16()) {
            return Err(Failure::QuotaExhausted);
        }
        if let Some(failure) = status_failure(&response) {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let answer: Answer = response.json().await.map_err(|e| other("answer", e))?;
        Ok(answer
            .results
            .into_iter()
            .map(|found| Row { url: found.url, title: found.title, snippet: found.content })
            .collect())
    }
}

#[derive(Deserialize)]
struct Answer {
    results: Vec<Found>,
}

#[derive(Deserialize)]
struct Found {
    url: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    content: String,
}

impl Provider for Tavily {
    fn name(&self) -> &'static str {
        "Tavily"
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
