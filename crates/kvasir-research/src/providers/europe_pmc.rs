//! Europe PMC's search, of the life sciences.

use kvasir_llm::BoxFuture;
use serde::Deserialize;

use super::{Failure, Provider, RESULTS, Row, client, keyless_failure, other, plain_text};

const ENDPOINT: &str = "https://www.ebi.ac.uk/europepmc/webservices/rest/search";

pub struct EuropePmc {
    endpoint: String,
    client: reqwest::Client,
}

impl Default for EuropePmc {
    fn default() -> Self {
        Self::at(ENDPOINT)
    }
}

impl EuropePmc {
    /// Only a test picks another endpoint.
    pub fn at(endpoint: &str) -> Self {
        Self { endpoint: endpoint.to_owned(), client: client() }
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let mut url = url::Url::parse(&self.endpoint).expect("the endpoint is a URL");
        url.query_pairs_mut()
            .append_pair("format", "json")
            .append_pair("query", query)
            .append_pair("pageSize", &RESULTS.to_string())
            // The abstracts are only in this one.
            .append_pair("resultType", "core");
        let response = self.client.get(url).send().await.map_err(|e| other("request", e))?;
        if let Some(failure) = keyless_failure(&response) {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let answer: Answer = response.json().await.map_err(|e| other("answer", e))?;
        Ok(answer.result_list.result.into_iter().filter_map(Article::into_row).collect())
    }
}

#[derive(Deserialize)]
struct Answer {
    #[serde(default, rename = "resultList")]
    result_list: ResultList,
}

#[derive(Default, Deserialize)]
struct ResultList {
    #[serde(default)]
    result: Vec<Article>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Article {
    id: Option<String>,
    source: Option<String>,
    doi: Option<String>,
    title: Option<String>,
    abstract_text: Option<String>,
}

impl Article {
    /// The DOI's page when it has one. An article with neither address is left out.
    fn into_row(self) -> Option<Row> {
        let url = match (self.doi, self.source, self.id) {
            (Some(doi), _, _) => format!("https://doi.org/{doi}"),
            (None, Some(source), Some(id)) => {
                format!("https://europepmc.org/article/{source}/{id}")
            }
            _ => return None,
        };
        Some(Row {
            url,
            title: self.title.as_deref().map_or_else(String::new, plain_text),
            snippet: self.abstract_text.as_deref().map_or_else(String::new, plain_text),
        })
    }
}

impl Provider for EuropePmc {
    fn name(&self) -> &'static str {
        "Europe PMC"
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}
