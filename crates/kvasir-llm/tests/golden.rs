//! Renders signatures that cover every field kind and checks each prompt is byte for byte what
//! dspy sent, in both formats, and that dspy's own replies parse to what dspy parsed them to.
//!
//! The signatures are copies of Python ones. The method crates that own them check all 32 against
//! `tests/golden/signatures.json` the same way.

use std::path::Path;

use kvasir_llm::adapter::{self, Format};
use kvasir_llm::shape::{Kind, Prop, Shape, literal_name, literal_schema, model_lax, model_schema};
use kvasir_llm::{Field, Message, Signature};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

macro_rules! choice {
    ($name:ident, $values:expr, $lax:expr) => {
        impl Shape for $name {
            fn type_name() -> String {
                literal_name($values)
            }
            fn kind() -> Kind {
                Kind::Literal($values)
            }
            fn schema(_: &mut Map<String, Value>) -> Value {
                literal_schema($values)
            }
            fn lax(value: &Value) -> Option<Value> {
                $lax(value)
            }
        }
    };
}

fn exact(values: &'static [&'static str]) -> impl Fn(&Value) -> Option<Value> {
    move |v| values.iter().any(|a| v == *a).then(|| v.clone())
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Form {
    Report,
    Ranking,
    Table,
}
const FORMS: &[&str] = &["report", "ranking", "table"];
choice!(Form, FORMS, exact(FORMS));

#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Priority {
    Core,
    Supporting,
    Peripheral,
}
const PRIORITIES: &[&str] = &["core", "supporting", "peripheral"];
// A priority is only an ordering hint, so a word outside the three fails no reply.
choice!(Priority, PRIORITIES, |v: &Value| Some(
    exact(PRIORITIES)(v).unwrap_or_else(|| "supporting".into())
));

#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Confidence {
    High,
    Medium,
    Low,
}
const CONFIDENCES: &[&str] = &["high", "medium", "low"];
// Models reach for "moderate" often enough that it fails whole replies.
choice!(Confidence, CONFIDENCES, |v: &Value| {
    exact(CONFIDENCES)(&if v == "moderate" { json!("medium") } else { v.clone() })
});

macro_rules! model {
    ($name:ident, $description:expr, $props:expr) => {
        impl Shape for $name {
            fn type_name() -> String {
                stringify!($name).into()
            }
            fn schema(defs: &mut Map<String, Value>) -> Value {
                model_schema(defs, stringify!($name), $description, &$props)
            }
            fn lax(value: &Value) -> Option<Value> {
                model_lax(value, &$props)
            }
        }
    };
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct NamedQuestion {
    title: String,
    question: String,
    priority: Priority,
}
model!(
    NamedQuestion,
    Some("A question, the short name a graph of the research shows it by, and how central it is."),
    [
        Prop::of::<String>("title")
            .describe("two to five words naming what the question asks about"),
        Prop::of::<String>("question"),
        Prop::of::<Priority>("priority")
            .describe(
                "core: the brief cannot be answered without it; supporting: it explains or \
                 qualifies the answer; peripheral: background a good answer can do without",
            )
            .default(json!("supporting")),
    ]
);

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct Section {
    title: String,
    questions: Vec<i64>,
    #[serde(default)]
    subsections: Vec<String>,
}
model!(
    Section,
    Some("One section of the report, and the questions whose findings it reports."),
    [
        Prop::of::<String>("title")
            .describe("two to six words, a heading a reader scans, not a question"),
        Prop::of::<Vec<i64>>("questions").describe("ids of the questions it reports on"),
        Prop::of::<Vec<String>>("subsections")
            .describe("two to four subheadings when the section covers distinct parts, else empty")
            .default_factory(json!([])),
    ]
);

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct Finding {
    claim: String,
    quote: String,
    confidence: Confidence,
}
model!(
    Finding,
    None,
    [
        Prop::of::<String>("claim"),
        Prop::of::<String>("quote"),
        Prop::of::<Confidence>("confidence")
    ]
);

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct ChosenCriterion {
    name: String,
    weight: i64,
}
model!(ChosenCriterion, None, [Prop::of::<String>("name"), Prop::of::<i64>("weight")]);

macro_rules! signature {
    ($sig:ident, $name:expr, $prompt:expr,
     in { $($i:ident: $it:ty = $id:expr),* $(,)? },
     out { $($o:ident: $ot:ty = $od:expr),* $(,)? } $(, $cot:ident)?) => {
        #[allow(non_camel_case_types, dead_code)]
        mod $sig {
            use super::*;
            #[derive(Serialize, Deserialize)]
            pub struct Input { $(pub $i: $it),* }
            #[derive(Debug, Deserialize)]
            pub struct Output { $(pub $o: $ot),* }
            pub struct Sig;
            impl Signature for Sig {
                const NAME: &'static str = $name;
                const INSTRUCTIONS: &'static str = include_str!(concat!("prompts/", $prompt, ".md"));
                type Input = Input;
                type Output = Output;
                fn inputs() -> Vec<Field> {
                    vec![$(Field::of::<$it>(stringify!($i), $id)),*]
                }
                fn outputs() -> Vec<Field> {
                    vec![$(Field::of::<$ot>(stringify!($o), $od)),*]
                }
            }
        }
    };
}

signature!(assess, "kvasir.methods.ledger.signatures.Assess", "assess",
    in { brief: String = "", entity: String = "the option", criterion: String = "",
         evidence: String = "one claim per line, prefixed by its id in brackets" },
    out { finding: String = "at most six words, or empty",
          claims: Vec<i64> = "ids of the claims the finding rests on" });
signature!(brief, "kvasir.methods.ledger.signatures.Brief", "brief",
    in { today: String = "today's date", question: String = "",
         clarifications: String = "questions asked and the answers given, or none" },
    out { brief: String = "one paragraph",
          assumptions: Vec<String> = "choices made on the author's behalf",
          output: Form = "ranking if the question asks which options are best, table if it asks to compare options without ranking them, report otherwise",
          criteria: Vec<String> = "the criteria the question says to compare or rank on, in its words, or empty",
          entities: Vec<String> = "the options the question names as the full set to compare, or empty if it leaves the set open" });
signature!(entities, "kvasir.methods.ledger.signatures.Entities", "entities",
    in { brief: String = "", background: String = "search results on the topic",
         count: i64 = "the most options to return" },
    out { entities: Vec<String> = "" });
signature!(criteria, "kvasir.methods.ledger.signatures.Criteria", "criteria",
    in { brief: String = "", background: String = "search results on the topic" },
    out { criteria: Vec<ChosenCriterion> = "three to five criteria" });
signature!(decompose, "kvasir.methods.ledger.signatures.Decompose", "decompose",
    in { brief: String = "" },
    out { questions: Vec<NamedQuestion> = "three to six questions" });
signature!(graph_outline, "kvasir.methods.graph.signatures.Outline", "graph_outline",
    in { brief: String = "",
         questions: String = "one question per line: [id] name: question (status, claims found); what is missing" },
    out { sections: Vec<Section> = "three to seven sections" });
signature!(intake, "kvasir.research.signatures.Intake", "intake",
    in { brief: String = "", question: String = "what the search that found the page was for",
         title: String = "", text: String = "a piece of the page" },
    out { findings: Vec<Finding> = "" });
signature!(check, "kvasir.research.evaluate.Check", "check",
    in { question: String = "", report: String = "", requirement: String = "" },
    out { met: bool = "", reason: String = "one short sentence" });
signature!(ask_question, "kvasir.storm.storm_wiki.modules.knowledge_curation.AskQuestion",
    "ask_question",
    in { topic: String = "topic you want to write about", conv: String = "conversation history" },
    out { reasoning: String = "", question: String = "a single question, or the closing thanks" });

fn golden() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/signatures.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// What Python's `capture.sample` gives a field.
fn sample(field: &Field) -> Value {
    match field.kind {
        Kind::Int => json!(3),
        _ => format!("the {}", field.name).into(),
    }
}

fn check<S: Signature>(golden: &Value, failures: &mut Vec<String>)
where
    S::Input: serde::de::DeserializeOwned,
{
    let case = &golden[S::NAME];
    let values: Map<String, Value> =
        S::inputs().iter().map(|f| (f.name.into(), sample(f))).collect();
    let input: S::Input = serde_json::from_value(Value::Object(values)).unwrap();
    let Value::Object(values) = serde_json::to_value(&input).unwrap() else { panic!() };
    for (format, prefix) in [(Format::Chat, ""), (Format::Json, "json_")] {
        let messages =
            adapter::render(format, S::INSTRUCTIONS, &S::inputs(), &S::outputs(), &values);
        let want: Vec<Message> =
            serde_json::from_value(case[format!("{prefix}messages")].clone()).unwrap();
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
        let reply = case[format!("{prefix}reply")].as_str().unwrap();
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

#[test]
fn prompts_and_parses_match_dspy() {
    let golden = golden();
    let mut failures = Vec::new();
    check::<assess::Sig>(&golden, &mut failures);
    check::<brief::Sig>(&golden, &mut failures);
    check::<entities::Sig>(&golden, &mut failures);
    check::<criteria::Sig>(&golden, &mut failures);
    check::<decompose::Sig>(&golden, &mut failures);
    check::<graph_outline::Sig>(&golden, &mut failures);
    check::<intake::Sig>(&golden, &mut failures);
    check::<check::Sig>(&golden, &mut failures);
    check::<ask_question::Sig>(&golden, &mut failures);
    assert!(failures.is_empty(), "{}", failures.join("\n\n"));
}

#[test]
fn before_validators_hold() {
    let fields = intake::Sig::outputs();
    let reply = r#"[[ ## findings ## ]]
[{"claim": "c", "quote": "q", "confidence": "moderate"}]"#;
    let parsed = adapter::parse(Format::Chat, &fields, reply).unwrap();
    assert_eq!(parsed["findings"][0]["confidence"], "medium");

    let fields = decompose::Sig::outputs();
    let reply = r#"{"questions": [{"title": "t", "question": "q", "priority": "urgent"},
        {"title": "u", "question": "r"}]}"#;
    let parsed = adapter::parse(Format::Json, &fields, reply).unwrap();
    assert_eq!(parsed["questions"][0]["priority"], "supporting");
    assert_eq!(parsed["questions"][1]["priority"], "supporting");

    let fields = graph_outline::Sig::outputs();
    let reply = "[[ ## sections ## ]]\n[{\"title\": \"t\", \"questions\": [\"1\"]}]";
    let parsed = adapter::parse(Format::Chat, &fields, reply).unwrap();
    assert_eq!(parsed["sections"][0], json!({"title": "t", "questions": [1], "subsections": []}));
}
