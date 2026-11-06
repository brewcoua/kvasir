//! Semantic Scholar's paper search. A key is optional: without one, requests share a public pool.

use std::time::Duration;

use kvasir_llm::BoxFuture;
use serde::Deserialize;

use super::{
    Failure, Provider, RESULTS, Row, client, keyless_failure, one_line, other, status_failure,
};

const ENDPOINT: &str = "https://api.semanticscholar.org/graph/v1/paper/search";

pub struct SemanticScholar {
    endpoint: String,
    key: Option<String>,
    client: reqwest::Client,
}

impl SemanticScholar {
    pub fn new(key: Option<&str>) -> Self {
        Self::at(ENDPOINT, key)
    }

    /// The endpoint is fixed in code, so no setting can send the key elsewhere. Only a test picks
    /// another.
    pub fn at(endpoint: &str, key: Option<&str>) -> Self {
        Self { endpoint: endpoint.to_owned(), key: key.map(str::to_owned), client: client() }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let mut url = url::Url::parse(&self.endpoint).expect("the endpoint is a URL");
        url.query_pairs_mut()
            .append_pair("query", query)
            .append_pair("limit", &RESULTS.to_string())
            .append_pair("fields", "title,abstract,url");
        let mut request = self.client.get(url);
        if let Some(key) = &self.key {
            request = request.header("x-api-key", key);
        }
        let response = request.send().await.map_err(|e| other("request", e))?;
        let failure = match self.key {
            Some(_) => status_failure(&response),
            None => keyless_failure(&response),
        };
        if let Some(failure) = failure {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let answer: Answer = response.json().await.map_err(|e| other("answer", e))?;
        Ok(answer
            .data
            .into_iter()
            .filter_map(|paper| {
                Some(Row {
                    url: paper.url?,
                    title: paper.title.unwrap_or_default(),
                    snippet: paper.summary.as_deref().map_or_else(String::new, one_line),
                })
            })
            .collect())
    }
}

#[derive(Deserialize)]
struct Answer {
    // Left out when nothing matched.
    #[serde(default)]
    data: Vec<Paper>,
}

/// Any of these may be null.
#[derive(Deserialize)]
struct Paper {
    url: Option<String>,
    title: Option<String>,
    #[serde(rename = "abstract")]
    summary: Option<String>,
}

impl Provider for SemanticScholar {
    fn name(&self) -> &'static str {
        "Semantic Scholar"
    }

    /// What a key allows. The public pool promises less.
    fn interval(&self) -> Duration {
        Duration::from_secs(1)
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
