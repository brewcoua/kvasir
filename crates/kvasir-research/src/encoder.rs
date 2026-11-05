//! Embeddings, and how close two of them are.

use std::sync::Arc;

use kvasir_config::Model;
use kvasir_llm::{Llm, LlmError, Sink};
use kvasir_run::model_key;

/// The embedding model.
#[derive(Clone)]
pub struct Encoder {
    pub llm: Arc<dyn Llm>,
    pub model: Model,
}

impl Encoder {
    /// The name pieces are cached under in `pages.db`: as litellm routed it, which the pieces
    /// Python stored carry.
    pub fn name(&self) -> String {
        model_key(&self.model)
    }

    /// One vector per text, in order. `query` marks texts searched with, against texts encoded
    /// without it; texts compared with each other are all encoded alike.
    pub async fn encode(
        &self,
        texts: &[String],
        query: bool,
        sink: &dyn Sink,
    ) -> Result<Vec<Vec<f32>>, LlmError> {
        if texts.is_empty() {
            return Ok(Vec::new());
        }
        self.llm.embed(&self.model, texts, query, sink).await
    }

    pub async fn one(
        &self,
        text: &str,
        query: bool,
        sink: &dyn Sink,
    ) -> Result<Vec<f32>, LlmError> {
        let mut vectors = self.encode(&[text.to_owned()], query, sink).await?;
        Ok(vectors.pop().unwrap_or_default())
    }
}

/// The cosine similarity of `a` and `b`; 0 when either is a zero vector, as sklearn gives it.
pub fn cosine(a: &[f32], b: &[f32]) -> f64 {
    let norm = |v: &[f32]| v.iter().map(|x| f64::from(*x).powi(2)).sum::<f64>().sqrt();
    let dot: f64 = a.iter().zip(b).map(|(x, y)| f64::from(*x) * f64::from(*y)).sum();
    let (na, nb) = (norm(a), norm(b));
    let na = if na == 0.0 { 1.0 } else { na };
    let nb = if nb == 0.0 { 1.0 } else { nb };
    dot / (na * nb)
}
