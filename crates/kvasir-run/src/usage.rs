//! What a model, a role or a stage spent, added up call by call.

use kvasir_types::runs::{ModelUsage, StageUsage};

use crate::round;

#[derive(Clone, Debug, Default)]
pub(crate) struct Usage {
    pub calls: u64,
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    pub reasoning_tokens: u64,
    pub cost: f64,
    pub embedding_tokens: u64,
    pub searches: u64,
    pub seconds: f64,
}

impl Usage {
    pub fn add(&mut self, prompt: u64, completion: u64, reasoning: u64, cost: f64) {
        self.calls += 1;
        self.prompt_tokens += prompt;
        self.completion_tokens += completion;
        self.reasoning_tokens += reasoning;
        self.cost += cost;
    }

    pub fn model(&self) -> ModelUsage {
        ModelUsage {
            calls: self.calls,
            prompt_tokens: self.prompt_tokens,
            completion_tokens: self.completion_tokens,
            reasoning_tokens: self.reasoning_tokens,
            cost: round(self.cost, 6),
            seconds: round(self.seconds, 3),
        }
    }

    pub fn stage(&self) -> StageUsage {
        StageUsage {
            model: self.model(),
            embedding_tokens: self.embedding_tokens,
            searches: self.searches,
        }
    }
}
