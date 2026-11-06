//! DuckDuckGo's page without JavaScript, read as SearXNG reads it. Against its terms: the operator
//! turns this on and carries that.

use std::sync::Arc;
use std::time::Duration;

use dom_query::Document;
use kvasir_llm::BoxFuture;
use reqwest::StatusCode;
use reqwest::header::{CONTENT_TYPE, REFERER};

use super::{Clearance, Cookies, Failure, Provider, Row, keyless_failure, one_line, other, scrape};

const NAME: &str = "DuckDuckGo";
const ENDPOINT: &str = "https://html.duckduckgo.com/html/";
const CHALLENGE_FORM: &str = r#"id="challenge-form""#;
/// Seen by others on a client it throttles; SearXNG only looks for the challenge form.
const ANOMALY: [&str; 2] = ["anomaly-modal__", "/anomaly.js"];

pub struct DuckDuckGo {
    endpoint: String,
    client: reqwest::Client,
    cookies: Arc<Cookies>,
}

impl DuckDuckGo {
    pub fn new(cookies: Arc<Cookies>) -> Self {
        Self::at(ENDPOINT, cookies)
    }

    /// Only a test picks another endpoint.
    pub fn at(endpoint: &str, cookies: Arc<Cookies>) -> Self {
        Self { endpoint: endpoint.to_owned(), client: scrape::client(), cookies }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let response = scrape::with_cookies(self.client.post(&self.endpoint), &self.cookies, NAME)
            .header(CONTENT_TYPE, "application/x-www-form-urlencoded")
            .header(REFERER, &self.endpoint)
            .body(scrape::form(&[("q", query), ("b", ""), ("kl", "wt-wt")]))
            .send()
            .await
            .map_err(|e| other("request", e))?;
        if let Some(failure) = keyless_failure(&response) {
            return Err(failure);
        }
        match response.status() {
            StatusCode::ACCEPTED => return Err(Failure::RateLimited { retry_after: None }),
            status if status.is_redirection() => {
                return Err(Failure::Other(format!("answered with a redirect: {status}")));
            }
            _ => {}
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let page = response.text().await.map_err(|e| other("answer", e))?;
        read(&page)
    }
}

fn blocked(_url: &str, page: &str) -> bool {
    page.contains(CHALLENGE_FORM) || ANOMALY.iter().any(|marker| page.contains(marker))
}

fn read(page: &str) -> Result<Vec<Row>, Failure> {
    if blocked("", page) {
        return Err(Failure::Captcha);
    }
    let document = Document::from(page);
    // Present even when nothing matched, so its absence is a page this does not understand.
    let links = document.select("div#links");
    if !links.exists() {
        return Err(Failure::Other("no result list in the page".into()));
    }
    let results = links.select("div.web-result:not(.result--ad)");
    Ok(results
        .iter()
        .filter_map(|result| {
            let link = result.select("h2 a");
            Some(Row {
                url: target(&link.attr("href")?),
                title: one_line(&link.text()),
                snippet: one_line(&result.select("a.result__snippet").text()),
            })
        })
        .collect())
}

/// The address behind `href`, which may go through DuckDuckGo's redirect as `/l/?uddg=<url>`.
fn target(href: &str) -> String {
    let absolute = if href.starts_with("//") { format!("https:{href}") } else { href.to_owned() };
    url::Url::parse(&absolute)
        .ok()
        .filter(|url| url.path() == "/l/")
        .and_then(|url| {
            url.query_pairs().find(|(key, _)| key == "uddg").map(|(_, to)| to.into_owned())
        })
        .unwrap_or_else(|| href.to_owned())
}

impl Provider for DuckDuckGo {
    fn name(&self) -> &'static str {
        NAME
    }

    fn clearance(&self, query: &str) -> Option<Clearance> {
        let mut url = url::Url::parse(&self.endpoint).ok()?;
        url.query_pairs_mut().append_pair("q", query);
        Some(Clearance { url: url.into(), blocked })
    }

    fn interval(&self) -> Duration {
        scrape::INTERVAL
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
