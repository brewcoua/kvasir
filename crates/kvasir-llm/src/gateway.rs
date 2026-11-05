//! The OpenAI-compatible HTTP client every provider is reached through.

use std::time::{Duration, Instant};

use backon::{BackoffBuilder, ExponentialBuilder};
use kvasir_config::Model;
use reqwest::StatusCode;
use serde_json::{Value, json};

use crate::{BoxFuture, Completion, Llm, LlmError, Request, Sink, Usage};

/// How long one attempt may take. A slow model writing a long section can take minutes.
pub const ATTEMPT_TIMEOUT: Duration = Duration::from_secs(600);

/// Calls providers over HTTP. Cheap to clone: clones share the connection pool.
#[derive(Debug, Clone)]
pub struct Gateway {
    http: reqwest::Client,
}

impl Gateway {
    pub fn new() -> Result<Self, reqwest::Error> {
        Ok(Self { http: reqwest::Client::builder().timeout(ATTEMPT_TIMEOUT).build()? })
    }

    async fn chat_once(&self, request: &Request<'_>) -> Result<Completion, LlmError> {
        let target = request.target;
        let mut body = json!({
            "model": target.model.name,
            "messages": request.messages,
            "max_tokens": target.max_tokens,
            "temperature": target.temperature,
        });
        if let Some(top_p) = target.top_p {
            body["top_p"] = top_p.into();
        }
        if let Some(effort) = &target.reasoning_effort {
            body["reasoning_effort"] = effort.as_str().into();
        }
        if request.json {
            body["response_format"] = json!({"type": "json_object"});
        }
        let (headers, reply) = self.post(&target.model, "chat/completions", &body).await?;
        let message = &reply["choices"][0]["message"];
        let text = |key: &str| message[key].as_str().map(str::to_owned);
        let usage = &reply["usage"];
        let count = |v: &Value| v.as_u64().unwrap_or(0);
        Ok(Completion {
            content: text("content"),
            reasoning: text("reasoning_content").or_else(|| text("reasoning")),
            usage: Usage {
                prompt_tokens: count(&usage["prompt_tokens"]),
                completion_tokens: count(&usage["completion_tokens"]),
                reasoning_tokens: count(&usage["completion_tokens_details"]["reasoning_tokens"]),
                cost: cost(&headers, usage),
            },
        })
    }

    async fn embed_once(
        &self,
        model: &Model,
        texts: &[String],
    ) -> Result<(Vec<Vec<f32>>, u64), LlmError> {
        let body = json!({"model": model.name, "input": texts});
        let (_, reply) = self.post(model, "embeddings", &body).await?;
        let bad = |message: &str| LlmError::Response {
            model: model.name.clone(),
            message: message.into(),
        };
        let mut rows: Vec<(u64, Vec<f32>)> = Vec::new();
        for (i, row) in reply["data"].as_array().ok_or_else(|| bad("no data"))?.iter().enumerate() {
            let vector = row["embedding"]
                .as_array()
                .and_then(|v| v.iter().map(|x| x.as_f64().map(|x| x as f32)).collect())
                .ok_or_else(|| bad("an embedding is not a list of numbers"))?;
            rows.push((row["index"].as_u64().unwrap_or(i as u64), vector));
        }
        rows.sort_by_key(|(index, _)| *index);
        let usage = &reply["usage"];
        let tokens =
            usage["total_tokens"].as_u64().or(usage["prompt_tokens"].as_u64()).unwrap_or(0);
        Ok((rows.into_iter().map(|(_, v)| v).collect(), tokens))
    }

    async fn post(
        &self,
        model: &Model,
        path: &str,
        body: &Value,
    ) -> Result<(reqwest::header::HeaderMap, Value), LlmError> {
        let transport = |source| LlmError::Transport { model: model.name.clone(), source };
        let response = self
            .http
            .post(format!("{}/{path}", model.api_base))
            .bearer_auth(&model.api_key)
            .json(body)
            .send()
            .await
            .map_err(transport)?;
        let status = response.status();
        let headers = response.headers().clone();
        let text = response.text().await.map_err(transport)?;
        if !status.is_success() {
            return Err(LlmError::Status { model: model.name.clone(), status, body: text });
        }
        let reply = serde_json::from_str(&text).map_err(|err| LlmError::Response {
            model: model.name.clone(),
            message: err.to_string(),
        })?;
        Ok((headers, reply))
    }
}

impl Llm for Gateway {
    fn chat<'a>(
        &'a self,
        request: Request<'a>,
        sink: &'a dyn Sink,
    ) -> BoxFuture<'a, Result<Completion, LlmError>> {
        Box::pin(retrying(request.retry_for, &request.target.model.name, move || {
            async move {
                // Per attempt, so a retry shows as the failure it followed.
                let call = sink.started(request.target, request.messages);
                let result = self.chat_once(&request).await;
                sink.ended(call, request.target, result.as_ref());
                result
            }
        }))
    }

    fn embed<'a>(
        &'a self,
        model: &'a Model,
        texts: &'a [String],
        query: bool,
        sink: &'a dyn Sink,
    ) -> BoxFuture<'a, Result<Vec<Vec<f32>>, LlmError>> {
        Box::pin(async move {
            let prefix = prefix(&model.name, query);
            let texts: Vec<String> = texts.iter().map(|t| format!("{prefix}{t}")).collect();
            let (vectors, tokens) =
                retrying(crate::RETRY_FOR, &model.name, || self.embed_once(model, &texts)).await?;
            sink.embedded(model, tokens);
            // A short answer would reach callers that index it positionally.
            if vectors.len() != texts.len() {
                return Err(LlmError::Count {
                    model: model.name.clone(),
                    got: vectors.len(),
                    sent: texts.len(),
                });
            }
            Ok(vectors)
        })
    }
}

/// What a text is prefixed with for models trained to be told what it is for: nomic-embed-text
/// tells a query from a document only by these.
fn prefix(model: &str, query: bool) -> &'static str {
    match (model.contains("nomic-embed-text"), query) {
        (true, true) => "search_query: ",
        (true, false) => "search_document: ",
        (false, _) => "",
    }
}

/// What the call cost, or 0.0 when nothing reports one. A LiteLLM proxy sends a header; Bifrost
/// puts a cost in the usage object, as a number or split into prompt and completion. A gateway
/// that reports nothing leaves the cost at zero, which is not the same as free.
fn cost(headers: &reqwest::header::HeaderMap, usage: &Value) -> f64 {
    let header = headers.get("x-litellm-response-cost").and_then(|v| v.to_str().ok());
    if let Some(cost) = header.and_then(|v| v.trim().parse::<f64>().ok()) {
        return cost;
    }
    let number = |v: &Value| v.as_f64().unwrap_or(0.0);
    match &usage["cost"] {
        Value::Object(split) => number(&split["prompt_cost"]) + number(&split["completion_cost"]),
        other => number(other),
    }
}

/// Runs `attempt` until it succeeds, fails with what a retry cannot fix, or `budget` is spent.
/// Waits double from a second, with jitter.
async fn retrying<T, F, Fut>(budget: Duration, model: &str, mut attempt: F) -> Result<T, LlmError>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<T, LlmError>>,
{
    let start = Instant::now();
    let mut waits = ExponentialBuilder::new()
        .with_min_delay(Duration::from_secs(1))
        .with_factor(2.0)
        .with_jitter()
        .without_max_delay()
        .without_max_times()
        .build();
    for tries in 1.. {
        let err = match attempt().await {
            Err(err) if err.retryable() => err,
            other => return other,
        };
        let left = budget.saturating_sub(start.elapsed());
        let wait = waits.next().unwrap_or(left).min(left);
        if wait.is_zero() {
            return Err(err);
        }
        tracing::warn!(
            model,
            error = %err,
            "gateway call failed, retrying in {:.1}s (attempt {tries})",
            wait.as_secs_f64()
        );
        tokio::time::sleep(wait).await;
    }
    unreachable!("the loop returns")
}

pub(crate) fn retryable(status: StatusCode) -> bool {
    status == StatusCode::TOO_MANY_REQUESTS
        || status == StatusCode::REQUEST_TIMEOUT
        || status.is_server_error()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prefixes() {
        assert_eq!(prefix("nomic-embed-text:latest", true), "search_query: ");
        assert_eq!(prefix("nomic-embed-text", false), "search_document: ");
        assert_eq!(prefix("text-embedding-3-small", true), "");
    }

    #[test]
    fn costs() {
        let mut headers = reqwest::header::HeaderMap::new();
        assert_eq!(cost(&headers, &json!({"cost": 0.5})), 0.5);
        assert_eq!(
            cost(&headers, &json!({"cost": {"prompt_cost": 0.25, "completion_cost": 0.5}})),
            0.75
        );
        assert_eq!(cost(&headers, &json!({})), 0.0);
        assert_eq!(cost(&headers, &json!({"cost": true})), 0.0);
        headers.insert("x-litellm-response-cost", "0.125".parse().unwrap());
        assert_eq!(cost(&headers, &json!({"cost": 0.5})), 0.125);
        headers.insert("x-litellm-response-cost", "n/a".parse().unwrap());
        assert_eq!(cost(&headers, &json!({"cost": 0.5})), 0.5);
    }
}
