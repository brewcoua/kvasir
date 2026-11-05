//! Checks a signature against `tests/golden/signatures.json`: each prompt byte for byte what dspy
//! sent, in both formats, and dspy's own replies parsed to what dspy parsed them to.

use std::path::Path;

use kvasir_llm::adapter::{self, Format};
use kvasir_llm::shape::Kind;
use kvasir_llm::{Field, Message, Signature};
use serde::de::DeserializeOwned;
use serde_json::{Map, Value, json};

/// The prompts and replies Python captured, by signature name.
pub fn golden() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/signatures.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("the golden file")).expect("JSON")
}

/// What Python's `capture.sample` gives a field.
fn sample(field: &Field) -> Value {
    match field.kind {
        Kind::Int => json!(3),
        _ => format!("the {}", field.name).into(),
    }
}

/// Adds to `failures` each way `S` differs from what dspy did.
pub fn check<S: Signature>(golden: &Value, failures: &mut Vec<String>)
where
    S::Input: DeserializeOwned,
{
    let case = &golden[S::NAME];
    if case.is_null() {
        failures.push(format!("{} is not in the golden file", S::NAME));
        return;
    }
    let values: Map<String, Value> =
        S::inputs().iter().map(|f| (f.name.into(), sample(f))).collect();
    let input: S::Input = serde_json::from_value(Value::Object(values)).expect("sample inputs");
    let Value::Object(values) = serde_json::to_value(&input).expect("inputs serialize") else {
        panic!("{} input is not a struct", S::NAME)
    };
    for (format, prefix) in [(Format::Chat, ""), (Format::Json, "json_")] {
        let messages =
            adapter::render(format, S::INSTRUCTIONS, &S::inputs(), &S::outputs(), &values);
        let want: Vec<Message> =
            serde_json::from_value(case[format!("{prefix}messages")].clone()).expect("messages");
        for (have, want) in messages.iter().zip(&want) {
            if have != want {
                let at = have
                    .content
                    .chars()
                    .zip(want.content.chars())
                    .take_while(|(a, b)| a == b)
                    .count();
                let around =
                    |s: &str| s.chars().skip(at.saturating_sub(40)).take(100).collect::<String>();
                failures.push(format!(
                    "{} {format:?} {} differs at {at}:\n  have {:?}\n  want {:?}",
                    S::NAME,
                    want.role,
                    around(&have.content),
                    around(&want.content)
                ));
            }
        }
        let reply = case[format!("{prefix}reply")].as_str().expect("a reply");
        let parsed = adapter::parse(format, &S::outputs(), reply);
        let want = case[format!("{prefix}parsed")].clone();
        match parsed {
            Some(map) if Value::Object(map.clone()) == want => {
                if let Err(err) = serde_json::from_value::<S::Output>(Value::Object(map)) {
                    failures.push(format!("{} {format:?} output: {err}", S::NAME));
                }
            }
            other => failures.push(format!("{} {format:?} parsed {other:?}, want {want}", S::NAME)),
        }
    }
}
