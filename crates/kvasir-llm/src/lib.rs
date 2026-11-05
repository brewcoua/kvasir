//! Calling language models: signatures rendered and parsed as dspy did, and an OpenAI-compatible
//! gateway client that retries what a retry can fix and reports what each call spent.

pub mod adapter;
pub mod shape;
pub mod signature;

use serde::{Deserialize, Serialize};

pub use signature::{Field, Signature};

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
