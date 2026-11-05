//! The most recent runs, in memory and, with `runs.store` set to sqlite, in the store.

use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use kvasir_db::DbError;
use kvasir_types::runs::{LmCallContent, RunDetail, RunSummary, SearchContent};

use crate::{Run, RunStore, State};

/// What a lookup did not find: the run never existed or aged out, or the call or search has no
/// content because keeping it is off, the id is unknown, or it aged out.
#[derive(Debug, thiserror::Error)]
pub enum NotFound {
    #[error("{0}")]
    Run(String),
    #[error("{0}")]
    Call(String),
    #[error("{0}")]
    Search(String),
    #[error(transparent)]
    Store(#[from] DbError),
}

/// The most recent runs, newest first. Memory holds the runs this process started; a store, when
/// there is one, holds those of earlier processes too and answers for any memory has dropped.
#[derive(Debug)]
pub struct RunRegistry {
    runs: Mutex<IndexMap<String, Arc<Run>>>,
    limit: usize,
    store: Option<RunStore>,
    keep_content: bool,
}

impl RunRegistry {
    pub fn new(limit: usize, store: Option<RunStore>, keep_content: bool) -> Self {
        Self { runs: Mutex::new(IndexMap::new()), limit, store, keep_content }
    }

    fn live(&self) -> Vec<Arc<Run>> {
        self.runs.lock().unwrap_or_else(|e| e.into_inner()).values().cloned().collect()
    }

    pub async fn start(&self, kind: &str, topic: &str, planned_stages: Vec<String>) -> Arc<Run> {
        let run = Arc::new(Run::new(kind, topic, planned_stages, self.keep_content));
        if let Some(store) = &self.store {
            store.save(&run).await;
        }
        let mut runs = self.runs.lock().unwrap_or_else(|e| e.into_inner());
        runs.insert(run.id.clone(), Arc::clone(&run));
        while runs.len() > self.limit {
            // Insertion-ordered, so this drops the oldest.
            runs.shift_remove_index(0);
        }
        run
    }

    /// Ends `run` and writes it to the store.
    pub async fn finish(&self, run: &Run, state: State, error: Option<String>) {
        run.finish(state, error);
        if let Some(store) = &self.store {
            store.save(run).await;
        }
    }

    /// A run this process holds in memory, the only kind that can be followed live.
    pub fn get(&self, id: &str) -> Option<Arc<Run>> {
        self.runs.lock().unwrap_or_else(|e| e.into_inner()).get(id).cloned()
    }

    pub async fn summaries(&self) -> Result<Vec<RunSummary>, DbError> {
        let mut summaries: Vec<RunSummary> =
            self.live().iter().rev().map(|run| run.summary()).collect();
        if let Some(store) = &self.store {
            let held: Vec<String> = summaries.iter().map(|s| s.id.clone()).collect();
            let stored = store.summaries().await?;
            summaries.extend(stored.into_iter().filter(|s| !held.contains(&s.id)));
            summaries.sort_by(|a, b| b.created_at.total_cmp(&a.created_at));
        }
        summaries.truncate(self.limit);
        Ok(summaries)
    }

    /// The runs whose result holds any of `urls` as a string, newest first. Reads the result of
    /// every run kept, there being no index.
    pub async fn citing(&self, urls: &[String]) -> Result<Vec<RunSummary>, DbError> {
        let live = self.live();
        let needles: Vec<String> =
            urls.iter().map(|url| serde_json::to_string(url).expect("serializes")).collect();
        let mut found: Vec<RunSummary> = live
            .iter()
            .filter(|run| {
                let blob = run.result().map(|r| serde_json::to_string(&r).expect("serializes"));
                blob.is_some_and(|blob| needles.iter().any(|needle| blob.contains(needle)))
            })
            .map(|run| run.summary())
            .collect();
        if let Some(store) = &self.store {
            let stored = store.citing(urls).await?;
            found.extend(stored.into_iter().filter(|s| !live.iter().any(|run| run.id == s.id)));
        }
        found.sort_by(|a, b| b.created_at.total_cmp(&a.created_at));
        Ok(found)
    }

    pub async fn detail(&self, id: &str) -> Result<RunDetail, NotFound> {
        if let Some(run) = self.get(id) {
            return Ok(run.detail());
        }
        let stored = match &self.store {
            Some(store) => store.detail(id).await?,
            None => None,
        };
        stored.ok_or_else(|| NotFound::Run(id.to_owned()))
    }

    pub async fn content(&self, id: &str, call: u64) -> Result<LmCallContent, NotFound> {
        let content = match (self.get(id), &self.store) {
            (Some(run), _) => run.content(call),
            (None, Some(store)) => store.content(id, call).await?,
            (None, None) => None,
        };
        content.ok_or_else(|| NotFound::Call(format!("{call} of run {id}")))
    }

    pub async fn search_content(&self, id: &str, search: u64) -> Result<SearchContent, NotFound> {
        let content = match (self.get(id), &self.store) {
            (Some(run), _) => run.search_content(search),
            (None, Some(store)) => store.search_content(id, search).await?,
            (None, None) => None,
        };
        content.ok_or_else(|| NotFound::Search(format!("{search} of run {id}")))
    }
}
