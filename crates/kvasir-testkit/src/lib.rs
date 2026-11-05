//! Stand-ins for the services kvasir calls, for tests: a scripted language model, and the golden
//! check of a signature.

pub mod golden;

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;

use kvasir_config::Model;
use kvasir_llm::{BoxFuture, Completion, Llm, LlmError, Message, Request, Sink, Usage};

type Script = Box<dyn Fn(&Request<'_>) -> Result<String, LlmError> + Send + Sync>;
/// Vectors by text, and the one for any other text.
type Vectors = (HashMap<String, Vec<f32>>, Option<Vec<f32>>);

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
    vectors: Option<Vectors>,
}

impl ScriptedLlm {
    pub fn new(
        script: impl Fn(&Request<'_>) -> Result<String, LlmError> + Send + Sync + 'static,
    ) -> Self {
        Self { script: Box::new(script), sent: Mutex::new(Vec::new()), vectors: None }
    }

    /// Replies with each of `replies` in turn, then fails the test.
    pub fn replying(replies: &[&str]) -> Self {
        let replies: Mutex<Vec<String>> =
            Mutex::new(replies.iter().rev().map(|r| (*r).to_owned()).collect());
        Self::new(move |_| Ok(replies.lock().unwrap().pop().expect("no reply left in the script")))
    }

    /// Replies to each role with its replies in turn, and fails the test when a role has none
    /// left, or none at all.
    pub fn by_role(replies: &[(&str, &[&str])]) -> Self {
        let queues: HashMap<String, VecDeque<String>> = replies
            .iter()
            .map(|(role, said)| {
                ((*role).to_owned(), said.iter().map(|r| (*r).to_owned()).collect())
            })
            .collect();
        let queues = Mutex::new(queues);
        Self::new(move |request| {
            let role = request.target.role;
            let mut queues = queues.lock().unwrap();
            let reply = queues.get_mut(role).and_then(VecDeque::pop_front);
            Ok(reply.unwrap_or_else(|| panic!("no reply left for {role}")))
        })
    }

    /// Embeds each text as the vector it is given here and any other as `default`, failing the
    /// test without one, instead of by its letters.
    pub fn with_vectors(mut self, vectors: &[(&str, &[f32])], default: Option<&[f32]>) -> Self {
        let vectors = vectors.iter().map(|(text, v)| ((*text).to_owned(), v.to_vec())).collect();
        self.vectors = Some((vectors, default.map(<[f32]>::to_vec)));
        self
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

    /// Counts of each letter, so texts sharing letters are near each other, unless vectors were
    /// given.
    fn embed<'a>(
        &'a self,
        model: &'a Model,
        texts: &'a [String],
        _query: bool,
        sink: &'a dyn Sink,
    ) -> BoxFuture<'a, Result<Vec<Vec<f32>>, LlmError>> {
        Box::pin(async move {
            sink.embedded(model, texts.len() as u64);
            let Some((vectors, default)) = &self.vectors else {
                return Ok(texts.iter().map(|t| letters(t)).collect());
            };
            let vector = |text: &String| {
                let found = vectors.get(text).or(default.as_ref());
                found.cloned().unwrap_or_else(|| panic!("no vector for {text:?}"))
            };
            Ok(texts.iter().map(vector).collect())
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

/// A reply in the chat format, its fields in order, each value as the model would write it.
pub fn reply(fields: &[(&str, &str)]) -> String {
    let mut text: String =
        fields.iter().map(|(name, value)| format!("[[ ## {name} ## ]]\n{value}\n\n")).collect();
    text += "[[ ## completed ## ]]";
    text
}
