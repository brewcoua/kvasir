//! The research steps the methods share: searching, reading pages into claims, recalling what was
//! read, writing sections, and checking what was written against its evidence.

pub mod claims;
pub mod clarify;
pub mod corpus;
pub mod corroborate;
pub mod encoder;
pub mod ground;
pub mod read;
pub mod render;
pub mod search;
pub mod signatures;
pub mod style;
pub mod text;
pub mod verify;
pub mod write;

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use kvasir_fetch::{FetchError, Page, PageStore, Web};
use kvasir_llm::{BoxFuture, Llm, LlmError, RETRY_FOR, Signature, Target};
use kvasir_run::RunCtx;
use kvasir_sites::sources::Sources;
use kvasir_types::sites::Tier;

pub use encoder::Encoder;
pub use search::{Hit, Search};

/// What a run's steps call: the models by role, the encoder, the search engine, the web and the
/// source classifier, with the settings they read.
#[derive(Clone)]
pub struct Tools {
    pub llm: Arc<dyn Llm>,
    /// By role: those of `kvasir_llm::roles::research`, and `ground` and `ground_intake` when the
    /// grounding checker is to check sentences and claims.
    pub targets: HashMap<&'static str, Target>,
    pub encoder: Encoder,
    pub search: Arc<dyn Search>,
    pub pages: Arc<dyn Pages>,
    pub sources: Option<Arc<dyn Classify>>,
    pub context_tokens: usize,
    /// How many calls one step makes side by side.
    pub max_threads: usize,
    /// Overrides `fetch.max_age_hours` for this run.
    pub max_age: Option<u32>,
}

impl Tools {
    pub fn target(&self, role: &str) -> &Target {
        self.targets.get(role).unwrap_or_else(|| panic!("no model for {role}"))
    }

    /// Calls the model of `role` with signature `S`.
    pub async fn predict<S: Signature>(
        &self,
        ctx: &RunCtx,
        role: &str,
        input: &S::Input,
    ) -> Result<S::Output, LlmError> {
        kvasir_llm::predict::<S>(&*self.llm, self.target(role), input, RETRY_FOR, ctx).await
    }
}

/// `result`, with a reply that did not parse as None: a step that cannot read one answer carries
/// on without it, while a model that cannot be reached ends the run.
pub fn readable<T>(result: Result<T, LlmError>) -> Result<Option<T>, LlmError> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(LlmError::Parse { signature, reply }) => {
            tracing::warn!("could not read {signature}: {}", kvasir_extract::head(&reply, 200));
            Ok(None)
        }
        Err(err) => Err(err),
    }
}

/// How far each host is trusted: the source classifier, or a stand-in in tests.
pub trait Classify: Send + Sync {
    /// Each of `hosts`, waiting at most `deadline` for what is not known yet.
    fn tiers<'a>(
        &'a self,
        hosts: Vec<String>,
        deadline: Duration,
    ) -> BoxFuture<'a, HashMap<String, Tier>>;
}

impl Classify for Arc<Sources> {
    fn tiers<'a>(
        &'a self,
        hosts: Vec<String>,
        deadline: Duration,
    ) -> BoxFuture<'a, HashMap<String, Tier>> {
        Box::pin(async move {
            let found = self.classify(&hosts, deadline).await;
            found.into_iter().map(|(host, source)| (host, source.tier())).collect()
        })
    }
}

/// How pages are read: the web, or a stand-in in tests.
pub trait Pages: Send + Sync {
    fn fetch<'a>(
        &'a self,
        url: &'a str,
        max_age: Option<u32>,
    ) -> BoxFuture<'a, Result<Page, FetchError>>;
    /// Whether a lead may be read, by its host and its site's robots.txt.
    fn allowed<'a>(&'a self, url: &'a str) -> BoxFuture<'a, bool>;
    /// Where pages and their pieces are cached, if anywhere.
    fn cache(&self) -> Option<PageStore>;
}

impl Pages for Web {
    fn fetch<'a>(
        &'a self,
        url: &'a str,
        max_age: Option<u32>,
    ) -> BoxFuture<'a, Result<Page, FetchError>> {
        Box::pin(self.fetch_page(url, max_age))
    }

    fn allowed<'a>(&'a self, url: &'a str) -> BoxFuture<'a, bool> {
        Box::pin(Web::allowed(self, url))
    }

    fn cache(&self) -> Option<PageStore> {
        Web::cache(self)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ResearchError {
    #[error(transparent)]
    Llm(#[from] LlmError),
    /// Searches kept failing, so the run stops rather than write from no evidence.
    #[error("{searches} searches in a row failed: {}", failures.join("; "))]
    SearchUnavailable { searches: usize, failures: Vec<String> },
}

impl ResearchError {
    pub fn unavailable(searches: usize, failures: Vec<String>) -> Self {
        let mut unique = Vec::new();
        for failure in failures {
            if !unique.contains(&failure) {
                unique.push(failure);
            }
        }
        Self::SearchUnavailable { searches, failures: unique }
    }
}
