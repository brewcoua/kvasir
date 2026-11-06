#![doc = include_str!("../README.md")]

pub mod adapter;
pub mod gateway;
mod macros;
pub mod roles;
pub mod shape;
pub mod signature;

use std::pin::Pin;
use std::time::Duration;

use kvasir_config::Model;
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use adapter::Format;
pub use gateway::Gateway;
pub use signature::{Field, Signature};

/// How long a call retries a gateway that keeps failing. A run is minutes of work, so waiting out
/// an outage costs less than losing it.
pub const RETRY_FOR: Duration = Duration::from_secs(1000);

/// How long a client's housekeeping call retries: a client asking for a chat title gives up long
/// before [`RETRY_FOR`].
pub const TASK_RETRY_FOR: Duration = Duration::from_secs(30);

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// One chat message.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Message {
    pub role: String,
    pub content: String,
}

impl Message {
    pub fn system(content: impl Into<String>) -> Self {
        Self { role: "system".into(), content: content.into() }
    }

    pub fn user(content: impl Into<String>) -> Self {
        Self { role: "user".into(), content: content.into() }
    }
}

/// A model as one role calls it.
#[derive(Debug, Clone, PartialEq)]
pub struct Target {
    pub model: Model,
    /// What the call is for, such as `intake`, which a run's usage is split by.
    pub role: &'static str,
    pub max_tokens: u32,
    pub temperature: f64,
    pub top_p: Option<f64>,
    /// Sent only when set.
    pub reasoning_effort: Option<String>,
}

/// One chat completion to make.
#[derive(Debug, Clone, Copy)]
pub struct Request<'a> {
    pub target: &'a Target,
    pub messages: &'a [Message],
    /// Asks for a JSON object, as dspy's JSON format does.
    pub json: bool,
    /// How long to keep retrying failures a retry can fix.
    pub retry_for: Duration,
}

/// What a call spent.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Usage {
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    /// The part of `completion_tokens` spent thinking.
    pub reasoning_tokens: u64,
    pub cost: f64,
}

/// A model's answer.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Completion {
    pub content: Option<String>,
    /// The thinking a gateway returns apart from the answer.
    pub reasoning: Option<String>,
    pub usage: Usage,
}

#[derive(Debug, thiserror::Error)]
pub enum LlmError {
    #[error("{model} answered {status}: {body}")]
    Status { model: String, status: StatusCode, body: String },
    #[error("{model} could not be reached: {source}")]
    Transport {
        model: String,
        #[source]
        source: reqwest::Error,
    },
    #[error("{model} sent an unreadable answer: {message}")]
    Response { model: String, message: String },
    #[error("{model} returned {got} embeddings for {sent} text(s)")]
    Count { model: String, got: usize, sent: usize },
    #[error("{signature}: the reply did not parse, in either format")]
    Parse { signature: &'static str, reply: String },
}

impl LlmError {
    /// Whether a later attempt can plausibly succeed: a rate limit, a server error, a timeout or a
    /// failed connection. A bad request or a context overflow fails the same way every time.
    pub fn retryable(&self) -> bool {
        match self {
            Self::Status { status, .. } => gateway::retryable(*status),
            Self::Transport { .. } => true,
            _ => false,
        }
    }
}

/// Where calls are reported: a run, or nothing for a call that belongs to none.
pub trait Sink: Send + Sync {
    /// A completion was sent. Returns the id `ended` is given.
    fn started(&self, target: &Target, messages: &[Message]) -> u64;
    /// It came back, or failed.
    fn ended(&self, call: u64, target: &Target, result: Result<&Completion, &LlmError>);
    /// An embedding request spent `tokens`.
    fn embedded(&self, model: &Model, tokens: u64);
}

/// Reports nowhere, for a client's housekeeping, which is part of no run's accounting.
#[derive(Debug, Clone, Copy, Default)]
pub struct NoSink;

impl Sink for NoSink {
    fn started(&self, _: &Target, _: &[Message]) -> u64 {
        0
    }
    fn ended(&self, _: u64, _: &Target, _: Result<&Completion, &LlmError>) {}
    fn embedded(&self, _: &Model, _: u64) {}
}

/// A language model provider: the gateway, or a scripted one in tests.
pub trait Llm: Send + Sync {
    fn chat<'a>(
        &'a self,
        request: Request<'a>,
        sink: &'a dyn Sink,
    ) -> BoxFuture<'a, Result<Completion, LlmError>>;

    /// One vector per text, in order. `query` marks texts searched with, against the texts they
    /// are compared to.
    fn embed<'a>(
        &'a self,
        model: &'a Model,
        texts: &'a [String],
        query: bool,
        sink: &'a dyn Sink,
    ) -> BoxFuture<'a, Result<Vec<Vec<f32>>, LlmError>>;
}

/// Calls `target` with signature `S`: in the chat format, then once more in the JSON format if the
/// reply does not parse, as dspy's `ChatAdapter` falls back. A failed call is not retried in the
/// other format.
pub async fn predict<S: Signature>(
    llm: &dyn Llm,
    target: &Target,
    input: &S::Input,
    retry_for: Duration,
    sink: &dyn Sink,
) -> Result<S::Output, LlmError> {
    let Value::Object(values) = serde_json::to_value(input).expect("inputs serialize") else {
        panic!("{} input is not a struct", S::NAME);
    };
    let (inputs, outputs) = (S::inputs(), S::outputs());
    let mut reply = String::new();
    for format in [Format::Chat, Format::Json] {
        let messages = adapter::render(format, S::INSTRUCTIONS, &inputs, &outputs, &values);
        let request =
            Request { target, messages: &messages, json: format == Format::Json, retry_for };
        reply = llm.chat(request, sink).await?.content.unwrap_or_default();
        let parsed = adapter::parse(format, &outputs, &reply)
            .and_then(|fields| serde_json::from_value(Value::Object(fields)).ok());
        if let Some(output) = parsed {
            return Ok(output);
        }
        tracing::debug!(signature = S::NAME, ?format, "reply did not parse");
    }
    Err(LlmError::Parse { signature: S::NAME, reply })
}
