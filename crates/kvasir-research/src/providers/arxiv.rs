//! arXiv's API. It answers in Atom, and reports a bad query as a feed with one error entry.

use std::time::Duration;

use kvasir_llm::BoxFuture;
use serde::Deserialize;

use super::{Failure, Provider, RESULTS, Row, client, keyless_failure, one_line, other};

const ENDPOINT: &str = "https://export.arxiv.org/api/query";
const ERROR_ID: &str = "/api/errors";

pub struct Arxiv {
    endpoint: String,
    client: reqwest::Client,
}

impl Default for Arxiv {
    fn default() -> Self {
        Self::at(ENDPOINT)
    }
}

impl Arxiv {
    /// Only a test picks another endpoint.
    pub fn at(endpoint: &str) -> Self {
        Self { endpoint: endpoint.to_owned(), client: client() }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let mut url = url::Url::parse(&self.endpoint).expect("the endpoint is a URL");
        url.query_pairs_mut()
            .append_pair("search_query", &format!("all:{query}"))
            .append_pair("max_results", &RESULTS.to_string());
        let response = self.client.get(url).send().await.map_err(|e| other("request", e))?;
        if let Some(failure) = keyless_failure(&response) {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let body = response.text().await.map_err(|e| other("answer", e))?;
        let feed: Feed =
            quick_xml::de::from_str(&body).map_err(|e| Failure::Other(format!("answer: {e}")))?;
        if let Some(error) = feed.entry.iter().find(|entry| entry.id.contains(ERROR_ID)) {
            return Err(Failure::Other(one_line(&error.summary)));
        }
        Ok(feed
            .entry
            .into_iter()
            .map(|entry| Row {
                // Its ids are addresses, in http.
                url: entry.id.replacen("http://", "https://", 1),
                title: one_line(&entry.title),
                snippet: one_line(&entry.summary),
            })
            .collect())
    }
}

#[derive(Deserialize)]
struct Feed {
    #[serde(default)]
    entry: Vec<Entry>,
}

#[derive(Deserialize)]
struct Entry {
    id: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    summary: String,
}

impl Provider for Arxiv {
    fn name(&self) -> &'static str {
        "arXiv"
    }

    /// arXiv's terms: one request every 3 seconds, on one connection.
    fn interval(&self) -> Duration {
        Duration::from_secs(3)
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
