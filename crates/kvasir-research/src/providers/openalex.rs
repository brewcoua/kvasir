//! OpenAlex's works search. Its key is in the URL, so no message may hold the URL.

use std::collections::HashMap;

use kvasir_llm::BoxFuture;
use serde::Deserialize;

use super::{Failure, Provider, RESULTS, Row, client, other, status_failure};

const ENDPOINT: &str = "https://api.openalex.org/works";

pub struct OpenAlex {
    endpoint: String,
    key: String,
    client: reqwest::Client,
}

impl OpenAlex {
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
            .append_pair("search", query)
            .append_pair("per-page", &RESULTS.to_string())
            .append_pair("api_key", &self.key);
        let response = self.client.get(url).send().await.map_err(|e| other("request", e))?;
        if let Some(failure) = status_failure(&response) {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let answer: Answer = response.json().await.map_err(|e| other("answer", e))?;
        Ok(answer.results.into_iter().filter_map(Work::into_row).collect())
    }
}

#[derive(Deserialize)]
struct Answer {
    #[serde(default)]
    results: Vec<Work>,
}

#[derive(Deserialize)]
struct Work {
    id: Option<String>,
    doi: Option<String>,
    display_name: Option<String>,
    primary_location: Option<Location>,
    abstract_inverted_index: Option<HashMap<String, Vec<usize>>>,
}

#[derive(Deserialize)]
struct Location {
    landing_page_url: Option<String>,
}

impl Work {
    /// The DOI's page, or else the publisher's, or else OpenAlex's own.
    fn into_row(self) -> Option<Row> {
        let landing = self.primary_location.and_then(|location| location.landing_page_url);
        Some(Row {
            url: self.doi.or(landing).or(self.id)?,
            title: self.display_name.unwrap_or_default(),
            snippet: self.abstract_inverted_index.map_or_else(String::new, |index| read(&index)),
        })
    }
}

/// An abstract from its index of each word's positions, the only form OpenAlex gives it in.
fn read(index: &HashMap<String, Vec<usize>>) -> String {
    let mut words: Vec<(usize, &str)> = index
        .iter()
        .flat_map(|(word, positions)| positions.iter().map(|at| (*at, word.as_str())))
        .collect();
    words.sort_unstable();
    words.into_iter().map(|(_, word)| word).collect::<Vec<_>>().join(" ")
}

impl Provider for OpenAlex {
    fn name(&self) -> &'static str {
        "OpenAlex"
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
