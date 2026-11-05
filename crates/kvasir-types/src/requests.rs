//! What the routes take, and the answers of the calls that are not runs.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::results::Output;

/// Body of `POST /v1/tasks`: OpenAI-shaped chat messages, forwarded to the fast model as-is. At
/// least one.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TaskRequest {
    pub messages: Vec<std::collections::BTreeMap<String, String>>,
}

/// Payload of `POST /v1/tasks`. One completion, unparsed.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TaskResponse {
    pub content: String,
}

/// One question `/v1/clarify` asked, and the user's answer to it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Clarification {
    pub question: String,
    pub answer: String,
}

/// Body of `POST /v1/clarify`. The question is not empty.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ClarifyRequest {
    pub question: String,
}

/// Payload of `POST /v1/clarify`. Empty when the question needs no clarifying.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ClarifyResponse {
    pub questions: Vec<String>,
}

/// What a run is about. For STORM, the question is the article's topic. It is not empty.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Input {
    pub question: String,
    /// Only a method that clarifies reads them.
    #[serde(default)]
    pub clarifications: Vec<Clarification>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Frontier {
    /// Searches the least tried question first.
    #[default]
    Breadth,
    /// Orders questions by a score from the claims held, merges repeated claims, and closes a
    /// branch that stops adding.
    Worth,
}

/// Options of the `ledger` method. The budgets fall back to the configured defaults.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LedgerOptions {
    #[serde(default)]
    pub output: Option<Output>,
    #[serde(default)]
    pub criteria: Option<Vec<String>>,
    #[serde(default)]
    pub entities: Option<Vec<String>>,
    #[serde(default)]
    pub max_searches: Option<u64>,
    #[serde(default)]
    pub results_per_query: Option<u64>,
    #[serde(default)]
    pub max_attempts: Option<u64>,
    #[serde(default)]
    pub follow_ups: Option<bool>,
    #[serde(default)]
    pub max_depth: Option<u64>,
    #[serde(default)]
    pub max_entities: Option<u64>,
    #[serde(default)]
    pub max_criteria: Option<u64>,
    #[serde(default)]
    pub frontier: Frontier,
    #[serde(default)]
    pub ground: Option<bool>,
    #[serde(default)]
    pub ground_intake: bool,
    #[serde(default)]
    pub corroborate: bool,
    #[serde(default)]
    pub contradict: bool,
    #[serde(default = "yes")]
    pub recall_past_runs: bool,
    #[serde(default)]
    pub max_age_hours: Option<u64>,
    #[serde(default)]
    pub model_fast: Option<String>,
    #[serde(default)]
    pub model_strong: Option<String>,
    #[serde(default)]
    pub model_reason: Option<String>,
}

/// Options of the `graph` method. The budgets fall back to the configured defaults.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GraphOptions {
    /// Minutes the research may search for, before the report is written. Above 0.
    #[serde(default = "twenty")]
    pub research_minutes: f64,
    #[serde(default)]
    pub max_searches: Option<u64>,
    #[serde(default)]
    pub results_per_query: Option<u64>,
    #[serde(default)]
    pub max_attempts: Option<u64>,
    #[serde(default)]
    pub max_depth: Option<u64>,
    #[serde(default)]
    pub ground: Option<bool>,
    #[serde(default)]
    pub ground_intake: bool,
    #[serde(default)]
    pub corroborate: bool,
    #[serde(default)]
    pub contradict: bool,
    #[serde(default = "yes")]
    pub recall_past_runs: bool,
    #[serde(default)]
    pub max_age_hours: Option<u64>,
    #[serde(default)]
    pub model_fast: Option<String>,
    #[serde(default)]
    pub model_strong: Option<String>,
    #[serde(default)]
    pub model_reason: Option<String>,
}

impl Default for GraphOptions {
    fn default() -> Self {
        serde_json::from_value(Value::Object(Map::new())).expect("every field has a default")
    }
}

/// Options of the `storm` method. Each falls back to the configured default when omitted.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StormOptions {
    #[serde(default)]
    pub search_top_k: Option<u64>,
    #[serde(default)]
    pub max_conv_turn: Option<u64>,
    #[serde(default)]
    pub max_perspective: Option<u64>,
    #[serde(default = "yes")]
    pub do_polish_article: bool,
    #[serde(default)]
    pub model_fast: Option<String>,
    #[serde(default)]
    pub model_strong: Option<String>,
}

impl Default for StormOptions {
    fn default() -> Self {
        serde_json::from_value(Value::Object(Map::new())).expect("every field has a default")
    }
}

fn yes() -> bool {
    true
}

fn twenty() -> f64 {
    20.0
}

/// Body of `POST /v1/runs`, one variant per method.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "method", rename_all = "lowercase")]
pub enum RunRequest {
    Ledger {
        input: Input,
        #[serde(default)]
        options: LedgerOptions,
    },
    Graph {
        input: Input,
        #[serde(default)]
        options: GraphOptions,
    },
    Storm {
        input: Input,
        #[serde(default)]
        options: StormOptions,
    },
}

/// One entry of `GET /v1/methods`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct MethodInfo {
    pub name: String,
    /// Whether a client should ask `/v1/clarify` before starting it.
    pub clarifies: bool,
    /// The stages a run with the default options goes through.
    pub stages: Vec<String>,
    /// The options' JSON Schema, with the configured defaults as each property's `default`.
    pub options: Map<String, Value>,
}
