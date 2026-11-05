//! `predict` with a scripted model: the chat format first, the JSON format as a fallback.

use std::time::Duration;

use kvasir_config::Model;
use kvasir_llm::{Field, LlmError, NoSink, Signature, Target, predict};
use kvasir_testkit::ScriptedLlm;
use serde::{Deserialize, Serialize};

struct Plan;

#[derive(Serialize)]
struct PlanIn {
    brief: String,
    count: i64,
}

#[derive(Debug, Deserialize, PartialEq)]
struct PlanOut {
    queries: Vec<String>,
}

impl Signature for Plan {
    const NAME: &'static str = "test.Plan";
    const INSTRUCTIONS: &'static str = "Plan searches.";
    type Input = PlanIn;
    type Output = PlanOut;
    fn inputs() -> Vec<Field> {
        vec![Field::of::<String>("brief", ""), Field::of::<i64>("count", "")]
    }
    fn outputs() -> Vec<Field> {
        vec![Field::of::<Vec<String>>("queries", "")]
    }
}

fn target() -> Target {
    Target {
        model: Model { name: "m".into(), api_base: "http://unused".into(), api_key: "none".into() },
        role: "plan",
        max_tokens: 500,
        temperature: 1.0,
        top_p: Some(0.9),
        reasoning_effort: None,
    }
}

async fn plan(llm: &ScriptedLlm) -> Result<PlanOut, LlmError> {
    let input = PlanIn { brief: "b".into(), count: 2 };
    predict::<Plan>(llm, &target(), &input, Duration::ZERO, &NoSink).await
}

#[tokio::test]
async fn a_chat_reply_parses() {
    let llm =
        ScriptedLlm::replying(&["[[ ## queries ## ]]\n[\"a\", \"b\"]\n\n[[ ## completed ## ]]"]);

    assert_eq!(plan(&llm).await.unwrap(), PlanOut { queries: vec!["a".into(), "b".into()] });
    let sent = llm.sent();
    assert_eq!(sent.len(), 1);
    assert!(
        sent[0].messages[1].content.starts_with("[[ ## brief ## ]]\nb\n\n[[ ## count ## ]]\n2")
    );
}

#[tokio::test]
async fn a_reply_that_does_not_parse_is_asked_again_as_json() {
    let llm = ScriptedLlm::replying(&["no sections here", "{\"queries\": [\"c\"]}"]);

    assert_eq!(plan(&llm).await.unwrap(), PlanOut { queries: vec!["c".into()] });
    let sent = llm.sent();
    assert_eq!(sent.iter().map(|s| s.json).collect::<Vec<_>>(), [false, true]);
    assert!(sent[1].messages[0].content.contains("Outputs will be a JSON object"));
}

#[tokio::test]
async fn two_bad_replies_are_a_parse_error() {
    let llm = ScriptedLlm::replying(&["no", "still no"]);

    let err = plan(&llm).await.unwrap_err();

    assert!(
        matches!(err, LlmError::Parse { signature: "test.Plan", ref reply } if reply == "still no")
    );
}

#[tokio::test]
async fn a_failed_call_is_not_retried_in_the_other_format() {
    let llm = ScriptedLlm::new(|_| {
        Err(LlmError::Response { model: "m".into(), message: "broken".into() })
    });

    assert!(matches!(plan(&llm).await, Err(LlmError::Response { .. })));
    assert_eq!(llm.sent().len(), 1);
}
