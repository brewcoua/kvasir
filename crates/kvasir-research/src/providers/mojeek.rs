//! The Mojeek search API. Its errors come in the answer's `status`, and its key in the URL, so no
//! message may hold the URL.

use kvasir_llm::BoxFuture;
use serde::Deserialize;

use super::{Failure, Provider, RESULTS, Row, client, other, status_failure};

const ENDPOINT: &str = "https://www.mojeek.com/search";
const OUT_OF_QUOTA: &str = "ERROR: Daily Limit Reached.";

pub struct Mojeek {
    endpoint: String,
    key: String,
    client: reqwest::Client,
}

impl Mojeek {
    pub fn new(key: &str) -> Self {
        Self::at(ENDPOINT, key)
    }

    /// The endpoint is fixed in code, so no setting can send the key elsewhere. Only a test picks
    /// another.
    pub fn at(endpoint: &str, key: &str) -> Self {
        Self { endpoint: endpoint.to_owned(), key: key.to_owned(), client: client() }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let mut url = url::Url::parse(&self.endpoint).expect("the endpoint is a URL");
        url.query_pairs_mut()
            .append_pair("fmt", "json")
            .append_pair("api_key", &self.key)
            .append_pair("q", query)
            .append_pair("t", &RESULTS.to_string());
        let response = self.client.get(url).send().await.map_err(|e| other("request", e))?;
        if let Some(failure) = status_failure(&response) {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let answer: Envelope = response.json().await.map_err(|e| other("answer", e))?;
        let answer = answer.response;
        if answer.status == OUT_OF_QUOTA {
            return Err(Failure::QuotaExhausted);
        }
        if answer.status != "OK" {
            return Err(Failure::Other(answer.status));
        }
        Ok(answer
            .results
            .into_iter()
            .map(|found| Row { url: found.url, title: found.title, snippet: found.desc })
            .collect())
    }
}

#[derive(Deserialize)]
struct Envelope {
    response: Answer,
}

#[derive(Deserialize)]
struct Answer {
    status: String,
    #[serde(default)]
    results: Vec<Found>,
}

#[derive(Deserialize)]
struct Found {
    url: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    desc: String,
}

impl Provider for Mojeek {
    fn name(&self) -> &'static str {
        "Mojeek"
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
