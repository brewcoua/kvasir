//! Whether a document supports a claim, asked of a small grounding checker.
//!
//! Tier 2 of verification: cheaper than asking the model to judge, and on the LLM-AggreFact
//! benchmark as accurate as a far larger model prompted to. MiniCheck models, such as
//! `bespoke-minicheck` on Ollama, read this exact prompt and answer Yes or No. A claim the checker
//! supports is attributed to its quote, not proven true; corroboration is what checks it against
//! other sources.

use kvasir_llm::{LlmError, Message, RETRY_FOR, Request, Target};
use kvasir_run::RunCtx;

use crate::Tools;

pub fn prompt(document: &str, claim: &str) -> String {
    format!("Document: {document}\nClaim: {claim}")
}

/// Whether `document` supports `claim`, by `checker`. An answer that is not Yes counts as No.
pub async fn grounded(
    tools: &Tools,
    ctx: &RunCtx,
    checker: &Target,
    document: &str,
    claim: &str,
) -> Result<bool, LlmError> {
    let messages = [Message::user(prompt(document, claim))];
    let request =
        Request { target: checker, messages: &messages, json: false, retry_for: RETRY_FOR };
    let reply = tools.llm.chat(request, ctx).await?.content.unwrap_or_default();
    Ok(reply.trim().to_lowercase().starts_with("yes"))
}
