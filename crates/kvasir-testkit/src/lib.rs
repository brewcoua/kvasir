//! Stand-ins for the services kvasir calls, for tests: a scripted language model.

use std::sync::Mutex;

use kvasir_config::Model;
use kvasir_llm::{BoxFuture, Completion, Llm, LlmError, Message, Request, Sink, Usage};

type Script = Box<dyn Fn(&Request<'_>) -> Result<String, LlmError> + Send + Sync>;

/// A sent request, as the script saw it.
#[derive(Debug, Clone, PartialEq)]
pub struct Sent {
    pub role: &'static str,
    pub messages: Vec<Message>,
    pub json: bool,
}

/// A language model whose replies a test writes. Each call reports to the sink as the gateway
/// does, with 1 prompt and 1 completion token.
pub struct ScriptedLlm {
    script: Script,
    sent: Mutex<Vec<Sent>>,
}

impl ScriptedLlm {
    pub fn new(
        script: impl Fn(&Request<'_>) -> Result<String, LlmError> + Send + Sync + 'static,
    ) -> Self {
        Self { script: Box::new(script), sent: Mutex::new(Vec::new()) }
    }

    /// Replies with each of `replies` in turn, then fails the test.
    pub fn replying(replies: &[&str]) -> Self {
        let replies: Mutex<Vec<String>> =
            Mutex::new(replies.iter().rev().map(|r| (*r).to_owned()).collect());
        Self::new(move |_| Ok(replies.lock().unwrap().pop().expect("no reply left in the script")))
    }

    /// What was sent, in order.
    pub fn sent(&self) -> Vec<Sent> {
        self.sent.lock().unwrap().clone()
    }
}

impl Llm for ScriptedLlm {
    fn chat<'a>(
        &'a self,
        request: Request<'a>,
        sink: &'a dyn Sink,
    ) -> BoxFuture<'a, Result<Completion, LlmError>> {
        Box::pin(async move {
            self.sent.lock().unwrap().push(Sent {
                role: request.target.role,
                messages: request.messages.to_vec(),
                json: request.json,
            });
            let call = sink.started(request.target, request.messages);
            let result = (self.script)(&request).map(|content| Completion {
                content: Some(content),
                reasoning: None,
                usage: Usage { prompt_tokens: 1, completion_tokens: 1, ..Usage::default() },
            });
            sink.ended(call, request.target, result.as_ref());
            result
        })
    }

    /// Counts of each letter, so texts sharing letters are near each other.
    fn embed<'a>(
        &'a self,
        model: &'a Model,
        texts: &'a [String],
        _query: bool,
        sink: &'a dyn Sink,
    ) -> BoxFuture<'a, Result<Vec<Vec<f32>>, LlmError>> {
        Box::pin(async move {
            sink.embedded(model, texts.len() as u64);
            Ok(texts.iter().map(|t| letters(t)).collect())
        })
    }
}

fn letters(text: &str) -> Vec<f32> {
    let mut counts = vec![0.0; 26];
    for b in text.bytes().filter(u8::is_ascii_alphabetic) {
        counts[usize::from(b.to_ascii_lowercase() - b'a')] += 1.0;
    }
    counts
}
