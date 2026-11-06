//! The Exa search API. It returns no snippet unless asked for page contents, so highlights are
//! asked for.

use kvasir_llm::BoxFuture;
use reqwest::StatusCode;
use serde::Deserialize;
use serde_json::json;

use super::{Failure, Provider, RESULTS, Row, client, other, status_failure};

const ENDPOINT: &str = "https://api.exa.ai/search";

pub struct Exa {
    endpoint: String,
    key: String,
    client: reqwest::Client,
}

impl Exa {
    pub fn new(key: &str) -> Self {
        Self::at(ENDPOINT, key)
    }

    /// The endpoint is fixed in code, so no setting can send the key elsewhere. Only a test picks
    /// another.
    pub fn at(endpoint: &str, key: &str) -> Self {
        Self { endpoint: endpoint.to_owned(), key: key.to_owned(), client: client() }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let body = json!({"query": query, "numResults": RESULTS, "contents": {"highlights": true}});
        let response = self
            .client
            .post(&self.endpoint)
            .header("x-api-key", &self.key)
            .json(&body)
            .send()
            .await
            .map_err(|e| other("request", e))?;
        if response.status() == StatusCode::PAYMENT_REQUIRED {
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
            .map(|found| Row {
                url: found.url,
                title: found.title.unwrap_or_default(),
                snippet: found.highlights.join(" "),
            })
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
    title: Option<String>,
    #[serde(default)]
    highlights: Vec<String>,
}

impl Provider for Exa {
    fn name(&self) -> &'static str {
        "Exa"
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
