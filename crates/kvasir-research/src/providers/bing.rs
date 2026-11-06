//! Bing's result page, read as SearXNG reads it. Against its terms: the operator turns this on and
//! carries that. SearXNG knows no marker for its blocks, so a block shows as a page with no list.

use std::time::Duration;

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use dom_query::Document;
use kvasir_llm::BoxFuture;

use super::{Failure, Provider, Row, keyless_failure, one_line, other, scrape};

const ENDPOINT: &str = "https://www.bing.com/search";
const CLICK: &str = "https://www.bing.com/ck/a?";

pub struct Bing {
    endpoint: String,
    client: reqwest::Client,
}

impl Default for Bing {
    fn default() -> Self {
        Self::at(ENDPOINT)
    }
}

impl Bing {
    /// Only a test picks another endpoint.
    pub fn at(endpoint: &str) -> Self {
        Self { endpoint: endpoint.to_owned(), client: scrape::client() }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let mut url = url::Url::parse(&self.endpoint).expect("the endpoint is a URL");
        url.query_pairs_mut().append_pair("q", query).append_pair("setlang", "en");
        let response = self.client.get(url).send().await.map_err(|e| other("request", e))?;
        if let Some(failure) = keyless_failure(&response) {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let page = response.text().await.map_err(|e| other("answer", e))?;
        read(&page)
    }
}

fn read(page: &str) -> Result<Vec<Row>, Failure> {
    let document = Document::from(page);
    let list = document.select("ol#b_results");
    if !list.exists() {
        return Err(Failure::Other("no result list in the page".into()));
    }
    let results = list.select("li.b_algo");
    Ok(results
        .iter()
        .filter_map(|result| {
            let link = result.select("h2 a");
            let title = one_line(&link.text());
            let href = link.attr("href").filter(|href| !href.is_empty())?;
            if title.is_empty() {
                return None;
            }
            // Decoration Bing puts inside the snippet.
            result.select("span.algoSlug_icon").remove();
            Some(Row { url: target(&href), title, snippet: one_line(&result.select("p").text()) })
        })
        .collect())
}

/// The address behind `href`, which may go through Bing's redirect as `u=a1<base64url>`.
fn target(href: &str) -> String {
    let behind =
        href.starts_with(CLICK).then(|| url::Url::parse(href).ok()).flatten().and_then(|url| {
            let (_, encoded) = url.query_pairs().find(|(key, _)| key == "u")?;
            let bytes = URL_SAFE_NO_PAD.decode(encoded.strip_prefix("a1")?).ok()?;
            String::from_utf8(bytes).ok()
        });
    behind.unwrap_or_else(|| href.to_owned())
}

impl Provider for Bing {
    fn name(&self) -> &'static str {
        "Bing"
    }

    fn interval(&self) -> Duration {
        scrape::INTERVAL
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
