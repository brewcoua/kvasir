//! English Wikipedia's search, which takes no key but wants to know who is asking.

use std::sync::Arc;
use std::time::Duration;

use kvasir_fetch::Web;
use kvasir_llm::BoxFuture;
use reqwest::header::USER_AGENT;
use serde::Deserialize;

use super::{Failure, Provider, RESULTS, Row, client, keyless_failure, other, plain_text};

const ENDPOINT: &str = "https://en.wikipedia.org/w/rest.php/v1/search/page";
const ARTICLES: &str = "https://en.wikipedia.org/wiki/";

pub struct Wikipedia {
    endpoint: String,
    web: Arc<Web>,
    client: reqwest::Client,
}

impl Wikipedia {
    pub fn new(web: Arc<Web>) -> Self {
        Self::at(ENDPOINT, web)
    }

    /// Only a test picks another endpoint.
    pub fn at(endpoint: &str, web: Arc<Web>) -> Self {
        Self { endpoint: endpoint.to_owned(), web, client: client() }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let mut url = url::Url::parse(&self.endpoint).expect("the endpoint is a URL");
        url.query_pairs_mut().append_pair("q", query).append_pair("limit", &RESULTS.to_string());
        // Asked at each request, so a new contact takes effect without rebuilding the providers.
        let request = self.client.get(url).header(USER_AGENT, self.web.user_agent());
        let response = request.send().await.map_err(|e| other("request", e))?;
        if let Some(failure) = keyless_failure(&response) {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let answer: Answer = response.json().await.map_err(|e| other("answer", e))?;
        Ok(answer.pages.into_iter().map(Page::into_row).collect())
    }
}

#[derive(Deserialize)]
struct Answer {
    #[serde(default)]
    pages: Vec<Page>,
}

#[derive(Deserialize)]
struct Page {
    key: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    excerpt: String,
    description: Option<String>,
}

impl Page {
    fn into_row(self) -> Row {
        let mut url = url::Url::parse(ARTICLES).expect("the base is a URL");
        url.path_segments_mut()
            .expect("the base is a URL with a path")
            .pop_if_empty()
            .push(&self.key);
        // The excerpt is HTML, marking up what matched.
        let excerpt = plain_text(&self.excerpt);
        let snippet =
            if excerpt.is_empty() { self.description.unwrap_or_default() } else { excerpt };
        Row { url: url.into(), title: self.title, snippet }
    }
}

impl Provider for Wikipedia {
    fn name(&self) -> &'static str {
        "Wikipedia"
    }

    /// Wikimedia asks for requests one at a time, which any interval gives.
    fn interval(&self) -> Duration {
        Duration::from_millis(100)
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
