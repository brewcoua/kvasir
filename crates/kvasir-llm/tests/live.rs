//! One real call, to check the client against a provider. Ignored by default; run with
//!
//! ```sh
//! KVASIR_LIVE_BASE=http://localhost:11434/v1 KVASIR_LIVE_MODEL=gpt-oss:20b \
//! KVASIR_LIVE_EMBEDDING=nomic-embed-text cargo test -p kvasir-llm --test live -- --ignored
//! ```

use std::sync::Mutex;

use kvasir_config::Model;
use kvasir_llm::{
    Completion, Field, Gateway, Llm, LlmError, Message, RETRY_FOR, Signature, Sink, Target, Usage,
    predict,
};
use serde::{Deserialize, Serialize};

struct Capital;

#[derive(Serialize)]
struct CapitalIn {
    country: String,
}

#[derive(Debug, Deserialize)]
struct CapitalOut {
    city: String,
    population_millions: f64,
    landlocked: bool,
}

impl Signature for Capital {
    const NAME: &'static str = "live.Capital";
    const INSTRUCTIONS: &'static str = "Name the capital of a country, with facts about it.";
    type Input = CapitalIn;
    type Output = CapitalOut;
    fn inputs() -> Vec<Field> {
        vec![Field::of::<String>("country", "")]
    }
    fn outputs() -> Vec<Field> {
        vec![
            Field::of::<String>("city", "the capital's name"),
            Field::of::<f64>("population_millions", "the city's population"),
            Field::of::<bool>("landlocked", "whether the country has no coast"),
        ]
    }
}

#[derive(Default)]
struct Spent(Mutex<Vec<Usage>>, Mutex<u64>);

impl Sink for Spent {
    fn started(&self, _: &Target, _: &[Message]) -> u64 {
        0
    }
    fn ended(&self, _: u64, _: &Target, result: Result<&Completion, &LlmError>) {
        if let Ok(completion) = result {
            self.0.lock().unwrap().push(completion.usage);
        }
    }
    fn embedded(&self, _: &Model, tokens: u64) {
        *self.1.lock().unwrap() += tokens;
    }
}

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("set {name}"))
}

#[tokio::test]
#[ignore = "needs a model server"]
async fn a_live_call_parses_and_records_usage() {
    let model = |name| Model { name, api_base: env("KVASIR_LIVE_BASE"), api_key: "none".into() };
    let target = Target {
        model: model(env("KVASIR_LIVE_MODEL")),
        role: "live",
        max_tokens: 2000,
        temperature: 1.0,
        top_p: Some(0.9),
        reasoning_effort: Some("low".into()),
    };
    let gateway = Gateway::new().unwrap();
    let spent = Spent::default();

    let out = predict::<Capital>(
        &gateway,
        &target,
        &CapitalIn { country: "France".into() },
        RETRY_FOR,
        &spent,
    )
    .await
    .unwrap();
    let embedding = model(env("KVASIR_LIVE_EMBEDDING"));
    let vectors = gateway.embed(&embedding, &["Paris".into()], true, &spent).await.unwrap();

    println!(
        "{out:?}, usage {:?}, embedding tokens {}",
        spent.0.lock().unwrap(),
        spent.1.lock().unwrap()
    );
    assert_eq!(out.city, "Paris");
    assert!(out.population_millions > 0.0 && !out.landlocked);
    let usage = spent.0.lock().unwrap();
    assert!(
        !usage.is_empty() && usage.iter().all(|u| u.prompt_tokens > 0 && u.completion_tokens > 0)
    );
    assert_eq!(vectors.len(), 1);
    assert!(vectors[0].len() > 100);
}
