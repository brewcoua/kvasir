//! A run as the registry serves it: its state, progress, model calls, searches and usage.

use indexmap::IndexMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::results::{ResearchQuestion, RunResult, SourceInfo};

/// Seconds since the Unix epoch, as Python's `time.time`.
pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

/// Payload of a `progress` event. The fields are documented in `docs/architecture.md`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Progress {
    pub stage: String,
    pub detail: String,
    /// When it was published.
    #[serde(default = "now")]
    pub at: f64,
    #[serde(default)]
    pub done: Option<u64>,
    #[serde(default)]
    pub total: Option<u64>,
    /// Its place in the run, numbered from 0.
    #[serde(default)]
    pub seq: Option<u64>,
    /// What step of the run this is: attempt, part, result, section, recall, repair; None for a
    /// log line.
    #[serde(default)]
    pub kind: Option<String>,
    /// The `seq` of the event this one sits under.
    #[serde(default)]
    pub parent: Option<u64>,
    #[serde(default)]
    pub question: Option<u64>,
    #[serde(default)]
    pub search: Option<u64>,
    /// The 1-based index of the page within its attempt.
    #[serde(default)]
    pub page: Option<u64>,
    /// Names a counter within its stage; its updates are one event.
    #[serde(default)]
    pub series: Option<String>,
    #[serde(default)]
    pub updated_at: Option<f64>,
}

impl Progress {
    pub fn new(stage: &str, detail: impl Into<String>) -> Self {
        Self {
            stage: stage.to_owned(),
            detail: detail.into(),
            at: now(),
            done: None,
            total: None,
            seq: None,
            kind: None,
            parent: None,
            question: None,
            search: None,
            page: None,
            series: None,
            updated_at: None,
        }
    }
}

/// Payload of an `error` event.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ErrorEvent {
    pub message: String,
}

/// What one model, or one role, spent on a run.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ModelUsage {
    pub calls: u64,
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    /// Part of `completion_tokens`, not in addition to it.
    pub reasoning_tokens: u64,
    pub cost: f64,
    /// Wall time of the calls that answered, summed.
    #[serde(default)]
    pub seconds: f64,
}

/// What one stage spent, including what is not a completion.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct StageUsage {
    #[serde(flatten)]
    pub model: ModelUsage,
    pub embedding_tokens: u64,
    pub searches: u64,
}

/// A run's totals, and the breakdowns worth having.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct RunUsage {
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    pub reasoning_tokens: u64,
    pub cost: f64,
    pub embedding_tokens: u64,
    pub searches: u64,
    pub by_model: IndexMap<String, ModelUsage>,
    pub by_role: IndexMap<String, ModelUsage>,
    /// Keyed by the stage the run was in when the call was made.
    pub by_stage: IndexMap<String, StageUsage>,
}

/// One completion request, while it is waiting and after it returns.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct LmCall {
    #[serde(default)]
    pub id: Option<u64>,
    /// The `seq` of the last event published in the work that made the call.
    #[serde(default)]
    pub event: Option<u64>,
    #[serde(default)]
    pub has_content: bool,
    pub role: Option<String>,
    pub model: String,
    pub started_at: f64,
    pub ended_at: Option<f64>,
    pub prompt_tokens: Option<u64>,
    pub completion_tokens: Option<u64>,
    pub failed: bool,
}

/// What one completion was sent and answered, kept with runs.keep_call_content.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct LmCallContent {
    /// As sent to the gateway, in the OpenAI chat shape.
    pub messages: Vec<Map<String, Value>>,
    pub response: Option<String>,
    #[serde(default)]
    pub reasoning: Option<String>,
}

/// One query sent to the search engine.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SearchCall {
    pub id: u64,
    pub event: Option<u64>,
    pub has_content: bool,
    pub engine: String,
    pub query: String,
    pub started_at: f64,
    pub ended_at: Option<f64>,
    /// What the engine returned, before the run drops what it already read.
    pub results: Option<u64>,
    /// Why it came back empty, when it failed rather than found nothing.
    pub failures: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SearchResult {
    pub title: String,
    pub url: String,
    pub snippet: String,
    #[serde(default)]
    pub source: Option<SourceInfo>,
}

/// What one search returned, kept with runs.keep_call_content.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SearchContent {
    pub results: Vec<SearchResult>,
}

/// One run, as `GET /v1/runs` lists it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RunSummary {
    pub id: String,
    pub kind: String,
    pub topic: String,
    pub state: String,
    pub stage: Option<String>,
    pub created_at: f64,
    pub started_at: Option<f64>,
    pub ended_at: Option<f64>,
    pub error: Option<String>,
    pub usage: RunUsage,
    pub planned_stages: Vec<String>,
    pub latest: Option<Progress>,
    pub has_result: bool,
}

/// One stretch of time spent in a stage. `ended_at` is None while the run is still in it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RunSpan {
    pub started_at: f64,
    pub ended_at: Option<f64>,
}

/// Time spent in one stage, summed over its spans.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RunStage {
    pub name: String,
    pub started_at: f64,
    pub ended_at: Option<f64>,
    pub seconds: f64,
    pub spans: Vec<RunSpan>,
}

/// A run's live state, as the `status` event carries it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RunStatus {
    #[serde(flatten)]
    pub summary: RunSummary,
    pub stages: Vec<RunStage>,
    /// The most recent completions, oldest first, including those in flight.
    pub calls: Vec<LmCall>,
    #[serde(default)]
    pub searches: Vec<SearchCall>,
    /// The questions a graph run has asked so far.
    #[serde(default)]
    pub questions: Vec<ResearchQuestion>,
}

/// One run, as `GET /v1/runs/{id}` returns it, with every call it kept.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RunDetail {
    #[serde(flatten)]
    pub status: RunStatus,
    pub events: Vec<Progress>,
    pub result: Option<RunResult>,
}

/// Payload of the `run` event, the first frame of every streaming response.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RunStarted {
    pub run_id: String,
}
