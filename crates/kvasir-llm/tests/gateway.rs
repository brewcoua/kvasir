//! The gateway client against a fake OpenAI-compatible server.

use std::sync::Mutex;
use std::time::Duration;

use kvasir_config::Model;
use kvasir_llm::{
    Completion, Gateway, Llm, LlmError, Message, NoSink, Request, Sink, Target, Usage,
};
use serde_json::{Value, json};
use wiremock::matchers::{body_partial_json, header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn target(server: &MockServer) -> Target {
    Target {
        model: Model {
            name: "m:7b".into(),
            api_base: format!("{}/v1", server.uri()),
            api_key: "k".into(),
        },
        role: "plan",
        max_tokens: 500,
        temperature: 1.0,
        top_p: Some(0.9),
        reasoning_effort: Some("none".into()),
    }
}

fn reply(content: &str) -> Value {
    json!({
        "choices": [{"message": {"role": "assistant", "content": content, "reasoning": "hm"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4,
                  "completion_tokens_details": {"reasoning_tokens": 2},
                  "cost": {"prompt_cost": 0.25, "completion_cost": 0.5}},
    })
}

#[derive(Default)]
struct Calls(Mutex<Vec<String>>);

impl Sink for Calls {
    fn started(&self, target: &Target, _: &[Message]) -> u64 {
        let mut calls = self.0.lock().unwrap();
        calls.push(format!("start {}", target.role));
        calls.len() as u64
    }
    fn ended(&self, call: u64, _: &Target, result: Result<&Completion, &LlmError>) {
        let outcome = match result {
            Ok(c) => format!("{} tokens", c.usage.prompt_tokens + c.usage.completion_tokens),
            Err(e) => format!("failed retryable={}", e.retryable()),
        };
        self.0.lock().unwrap().push(format!("end {call} {outcome}"));
    }
    fn embedded(&self, model: &Model, tokens: u64) {
        self.0.lock().unwrap().push(format!("embedded {} {tokens}", model.name));
    }
}

#[tokio::test]
async fn a_chat_call_sends_the_role_settings_and_reads_usage_and_cost() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/chat/completions"))
        .and(header("authorization", "Bearer k"))
        .and(body_partial_json(json!({
            "model": "m:7b", "max_tokens": 500, "temperature": 1.0, "top_p": 0.9,
            "reasoning_effort": "none", "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": "hi"}],
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(reply("{}")))
        .expect(1)
        .mount(&server)
        .await;
    let target = target(&server);
    let messages = [Message::user("hi")];
    let request =
        Request { target: &target, messages: &messages, json: true, retry_for: Duration::ZERO };

    let completion = Gateway::new().unwrap().chat(request, &NoSink).await.unwrap();

    assert_eq!(
        completion,
        Completion {
            content: Some("{}".into()),
            reasoning: Some("hm".into()),
            usage: Usage {
                prompt_tokens: 10,
                completion_tokens: 4,
                reasoning_tokens: 2,
                cost: 0.75
            },
        }
    );
}

#[tokio::test]
async fn a_server_error_is_retried_and_each_attempt_reported() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(503))
        .up_to_n_times(1)
        .mount(&server)
        .await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(reply("ok"))
                .insert_header("x-litellm-response-cost", "0.125"),
        )
        .mount(&server)
        .await;
    let target = target(&server);
    let calls = Calls::default();
    let request =
        Request { target: &target, messages: &[], json: false, retry_for: Duration::from_secs(10) };

    let completion = Gateway::new().unwrap().chat(request, &calls).await.unwrap();

    assert_eq!(completion.usage.cost, 0.125);
    assert_eq!(
        *calls.0.lock().unwrap(),
        ["start plan", "end 1 failed retryable=true", "start plan", "end 3 14 tokens"]
    );
}

#[tokio::test]
async fn a_bad_request_fails_at_once() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(400).set_body_string("context length exceeded"))
        .expect(1)
        .mount(&server)
        .await;
    let target = target(&server);
    let request =
        Request { target: &target, messages: &[], json: false, retry_for: Duration::from_secs(60) };

    let err = Gateway::new().unwrap().chat(request, &NoSink).await.unwrap_err();

    assert!(matches!(err, LlmError::Status { status, .. } if status == 400), "{err}");
}

#[tokio::test]
async fn retrying_stops_when_the_budget_is_spent() {
    let server = MockServer::start().await;
    Mock::given(method("POST")).respond_with(ResponseTemplate::new(429)).mount(&server).await;
    let target = target(&server);
    let request =
        Request { target: &target, messages: &[], json: false, retry_for: Duration::ZERO };

    let err = Gateway::new().unwrap().chat(request, &NoSink).await.unwrap_err();

    assert!(err.retryable());
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn embeddings_come_back_in_input_order_with_prefixes_and_tokens() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/embeddings"))
        .and(body_partial_json(json!({"input": ["search_query: a", "search_query: b"]})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0, 0.5]}],
            "usage": {"prompt_tokens": 6, "total_tokens": 6},
        })))
        .mount(&server)
        .await;
    let model = Model {
        name: "nomic-embed-text".into(),
        api_base: format!("{}/v1", server.uri()),
        api_key: "none".into(),
    };
    let calls = Calls::default();

    let vectors = Gateway::new()
        .unwrap()
        .embed(&model, &["a".into(), "b".into()], true, &calls)
        .await
        .unwrap();

    assert_eq!(vectors, [vec![1.0, 0.5], vec![2.0]]);
    assert_eq!(*calls.0.lock().unwrap(), ["embedded nomic-embed-text 6"]);
}

#[tokio::test]
async fn a_short_embedding_answer_is_an_error() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "data": [{"index": 0, "embedding": [1.0]}],
        })))
        .mount(&server)
        .await;
    let model = Model { name: "e".into(), api_base: server.uri(), api_key: "none".into() };

    let err = Gateway::new()
        .unwrap()
        .embed(&model, &["a".into(), "b".into()], false, &NoSink)
        .await
        .unwrap_err();

    assert!(matches!(err, LlmError::Count { got: 1, sent: 2, .. }));
}
