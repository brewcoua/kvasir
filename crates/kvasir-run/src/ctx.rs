//! A line of work within a run: what it reports into, and the event it last published.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use kvasir_config::Model;
use kvasir_llm::{Completion, LlmError, Message, Sink, Target};
use kvasir_types::runs::{Progress, SearchResult};
use serde_json::{Map, Value};

use crate::Run;

const NO_EVENT: u64 = u64::MAX;

/// Cloned for each task a run fans out to, as Python copied its context into each pool task: a
/// call made in the task names the event published before it was handed out, and the task's own
/// events do not move its parent's.
#[derive(Debug)]
pub struct RunCtx {
    pub run: Arc<Run>,
    event: AtomicU64,
}

impl Clone for RunCtx {
    fn clone(&self) -> Self {
        Self {
            run: Arc::clone(&self.run),
            event: AtomicU64::new(self.event.load(Ordering::Relaxed)),
        }
    }
}

/// How a model is named in usage and calls: as litellm named it, which stored runs and
/// `tests/contract/shapes.json` key by.
pub fn model_key(model: &Model) -> String {
    format!("openai/{}", model.name)
}

impl RunCtx {
    pub fn new(run: Arc<Run>) -> Self {
        Self { run, event: AtomicU64::new(NO_EVENT) }
    }

    /// The `seq` of the last event this line of work published.
    pub fn event(&self) -> Option<u64> {
        Some(self.event.load(Ordering::Relaxed)).filter(|&seq| seq != NO_EVENT)
    }

    /// Records `event` on the run, logs it, and makes it the one later calls here follow.
    pub fn publish(&self, event: Progress) -> u64 {
        tracing::info!(run_id = %self.run.id, stage = %event.stage, "{}", event.detail);
        let seq = self.run.record_progress(event);
        self.event.store(seq, Ordering::Relaxed);
        seq
    }

    /// Logs a line under the current stage and event, which later calls do not follow.
    pub fn note(&self, detail: String) {
        tracing::info!(run_id = %self.run.id, "{detail}");
        if let Some(stage) = self.run.current_stage() {
            let event = Progress { parent: self.event(), ..Progress::new(&stage, detail) };
            self.run.record_progress(event);
        }
    }

    pub fn search_started(&self, engine: &str, query: &str) -> u64 {
        self.run.start_search(engine, query, self.event())
    }

    pub fn search_ended(&self, search: u64, results: Vec<SearchResult>, failures: Vec<String>) {
        self.run.end_search(search, results, failures);
    }
}

impl Sink for RunCtx {
    fn started(&self, target: &Target, messages: &[Message]) -> u64 {
        let messages = messages
            .iter()
            .map(|message| {
                let mut sent = Map::new();
                sent.insert("role".into(), Value::String(message.role.clone()));
                sent.insert("content".into(), Value::String(message.content.clone()));
                sent
            })
            .collect();
        self.run.start_lm(&model_key(&target.model), target.role, messages, self.event())
    }

    fn ended(&self, call: u64, target: &Target, result: Result<&Completion, &LlmError>) {
        match result {
            Ok(completion) => {
                let usage = completion.usage;
                self.run.end_lm(
                    call,
                    Some((usage.prompt_tokens, usage.completion_tokens)),
                    completion.content.clone(),
                    completion.reasoning.clone(),
                );
                self.run.record_lm(&model_key(&target.model), target.role, usage);
            }
            Err(_) => self.run.end_lm(call, None, None, None),
        }
    }

    fn embedded(&self, _model: &Model, tokens: u64) {
        self.run.record_embedding(tokens);
    }
}
