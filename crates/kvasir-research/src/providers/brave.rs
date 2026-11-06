//! The Brave Search API. Its documentation does not list its error codes, so a refused key and a
//! rate limit are read as on any other API.

use kvasir_llm::BoxFuture;
use serde::Deserialize;

use super::{Failure, Provider, RESULTS, Row, client, other, status_failure};

const ENDPOINT: &str = "https://api.search.brave.com/res/v1/web/search";

pub struct Brave {
    endpoint: String,
    key: String,
    client: reqwest::Client,
}

impl Brave {
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
        url.query_pairs_mut().append_pair("q", query).append_pair("count", &RESULTS.to_string());
        let response = self
            .client
            .get(url)
            .header("X-Subscription-Token", &self.key)
            .send()
            .await
            .map_err(|e| other("request", e))?;
        if let Some(failure) = status_failure(&response) {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let answer: Answer = response.json().await.map_err(|e| other("answer", e))?;
        Ok(answer
            .web
            .results
            .into_iter()
            .map(|found| Row { url: found.url, title: found.title, snippet: found.description })
            .collect())
    }
}

#[derive(Deserialize)]
struct Answer {
    // Left out when nothing was found.
    #[serde(default)]
    web: Web,
}

#[derive(Default, Deserialize)]
struct Web {
    #[serde(default)]
    results: Vec<Found>,
}

#[derive(Deserialize)]
struct Found {
    url: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    description: String,
}

impl Provider for Brave {
    fn name(&self) -> &'static str {
        "Brave"
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
