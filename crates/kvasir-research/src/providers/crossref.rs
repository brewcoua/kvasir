//! Crossref's works API. A `mailto` moves requests to its polite pool, with a higher limit.

use std::time::Duration;

use kvasir_llm::BoxFuture;
use serde::Deserialize;

use super::{
    Failure, Provider, RESULTS, Row, client, keyless_failure, one_line, other, plain_text,
};

const ENDPOINT: &str = "https://api.crossref.org/works";

pub struct Crossref {
    endpoint: String,
    mailto: Option<String>,
    client: reqwest::Client,
}

impl Crossref {
    pub fn new(mailto: Option<&str>) -> Self {
        Self::at(ENDPOINT, mailto)
    }

    /// Only a test picks another endpoint.
    pub fn at(endpoint: &str, mailto: Option<&str>) -> Self {
        Self { endpoint: endpoint.to_owned(), mailto: mailto.map(str::to_owned), client: client() }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let mut url = url::Url::parse(&self.endpoint).expect("the endpoint is a URL");
        url.query_pairs_mut().append_pair("query", query).append_pair("rows", &RESULTS.to_string());
        if let Some(mailto) = &self.mailto {
            url.query_pairs_mut().append_pair("mailto", mailto);
        }
        let response = self.client.get(url).send().await.map_err(|e| other("request", e))?;
        if let Some(failure) = keyless_failure(&response) {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let answer: Answer = response.json().await.map_err(|e| other("answer", e))?;
        Ok(answer
            .message
            .items
            .into_iter()
            .map(|item| Row {
                url: item.url,
                title: item.title.first().map_or_else(String::new, |title| one_line(title)),
                // In JATS, an XML of its own.
                snippet: item.summary.as_deref().map_or_else(String::new, plain_text),
            })
            .collect())
    }
}

#[derive(Deserialize)]
struct Answer {
    message: Message,
}

#[derive(Deserialize)]
struct Message {
    #[serde(default)]
    items: Vec<Item>,
}

#[derive(Deserialize)]
struct Item {
    #[serde(rename = "URL")]
    url: String,
    #[serde(default)]
    title: Vec<String>,
    #[serde(rename = "abstract")]
    summary: Option<String>,
}

impl Provider for Crossref {
    fn name(&self) -> &'static str {
        "Crossref"
    }

    /// Since 2025-12: searches 1 a second, or 3 with a `mailto`, which also allows 3 at a time
    /// where this asks one.
    fn interval(&self) -> Duration {
        if self.mailto.is_some() { Duration::from_millis(334) } else { Duration::from_secs(1) }
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
