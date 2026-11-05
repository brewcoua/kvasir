//! The evidence a run gathers, and lookups over it by meaning.

use kvasir_llm::{LlmError, Sink};
pub use kvasir_types::results::{Confidence, ResearchClaim as Claim};

use crate::encoder::{Encoder, cosine};

/// The id for the next claim: past every one held, even after claims were dropped.
pub fn next_id(claims: &[Claim]) -> u64 {
    claims.iter().map(|claim| claim.id).max().unwrap_or(0) + 1
}

/// Indices of the `k` texts closest to `query`, in their original order.
pub async fn closest(
    encoder: &Encoder,
    query: &str,
    texts: &[String],
    k: usize,
    sink: &dyn Sink,
) -> Result<Vec<usize>, LlmError> {
    if texts.len() <= k {
        return Ok((0..texts.len()).collect());
    }
    nearest(encoder, query, &encoder.encode(texts, false, sink).await?, k, sink).await
}

/// [`closest`], over texts already encoded, for when many queries search the same texts.
pub async fn nearest(
    encoder: &Encoder,
    query: &str,
    vectors: &[Vec<f32>],
    k: usize,
    sink: &dyn Sink,
) -> Result<Vec<usize>, LlmError> {
    if vectors.len() <= k {
        return Ok((0..vectors.len()).collect());
    }
    let wanted = encoder.one(query, true, sink).await?;
    let scores: Vec<f64> = vectors.iter().map(|vector| cosine(&wanted, vector)).collect();
    let mut picked = best_first(&scores);
    picked.truncate(k);
    picked.sort_unstable();
    Ok(picked)
}

/// Indices of `scores`, highest first; of equal scores, the later first, as numpy's reversed
/// `argsort` gives them.
pub(crate) fn best_first(scores: &[f64]) -> Vec<usize> {
    let mut order: Vec<usize> = (0..scores.len()).collect();
    order.sort_by(|&a, &b| scores[a].total_cmp(&scores[b]));
    order.reverse();
    order
}

pub fn listed<'a>(claims: impl IntoIterator<Item = &'a Claim>) -> String {
    let lines: Vec<String> =
        claims.into_iter().map(|claim| format!("[{}] {}", claim.id, claim.text)).collect();
    lines.join("\n")
}

pub fn quoted_claims<'a>(claims: impl IntoIterator<Item = &'a Claim>) -> String {
    let lines: Vec<String> = claims
        .into_iter()
        .map(|claim| format!("[{}] {}\n    quote: {}", claim.id, claim.text, claim.quote))
        .collect();
    lines.join("\n")
}
