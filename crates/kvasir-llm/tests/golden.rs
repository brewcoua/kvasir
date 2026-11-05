//! Renders signatures that cover every field kind and checks each prompt is byte for byte what
//! dspy sent, in both formats, and that dspy's own replies parse to what dspy parsed them to.
//!
//! The signatures are copies of Python ones. The method crates that own them check all 32 against
//! `tests/golden/signatures.json` the same way.

use kvasir_llm::adapter::{self, Format};
use kvasir_llm::shape::{Prop, exact};
use kvasir_llm::{Signature, choice, model, signature};
use kvasir_testkit::golden::{check, golden};
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Form {
    Report,
    Ranking,
    Table,
}
const FORMS: &[&str] = &["report", "ranking", "table"];
choice!(Form, FORMS, |v: &Value| exact(FORMS, v));

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
    exact(PRIORITIES, v).unwrap_or_else(|| "supporting".into())
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
    exact(CONFIDENCES, &if v == "moderate" { json!("medium") } else { v.clone() })
});

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

signature!(assess, "kvasir.methods.ledger.signatures.Assess", include_str!("prompts/assess.md"),
    in { brief: String = "", entity: String = "the option", criterion: String = "",
         evidence: String = "one claim per line, prefixed by its id in brackets" },
    out { finding: String = "at most six words, or empty",
          claims: Vec<i64> = "ids of the claims the finding rests on" });
signature!(brief, "kvasir.methods.ledger.signatures.Brief", include_str!("prompts/brief.md"),
    in { today: String = "today's date", question: String = "",
         clarifications: String = "questions asked and the answers given, or none" },
    out { brief: String = "one paragraph",
          assumptions: Vec<String> = "choices made on the author's behalf",
          output: Form = "ranking if the question asks which options are best, table if it asks to compare options without ranking them, report otherwise",
          criteria: Vec<String> = "the criteria the question says to compare or rank on, in its words, or empty",
          entities: Vec<String> = "the options the question names as the full set to compare, or empty if it leaves the set open" });
signature!(entities, "kvasir.methods.ledger.signatures.Entities", include_str!("prompts/entities.md"),
    in { brief: String = "", background: String = "search results on the topic",
         count: i64 = "the most options to return" },
    out { entities: Vec<String> = "" });
signature!(criteria, "kvasir.methods.ledger.signatures.Criteria", include_str!("prompts/criteria.md"),
    in { brief: String = "", background: String = "search results on the topic" },
    out { criteria: Vec<ChosenCriterion> = "three to five criteria" });
signature!(decompose, "kvasir.methods.ledger.signatures.Decompose", include_str!("prompts/decompose.md"),
    in { brief: String = "" },
    out { questions: Vec<NamedQuestion> = "three to six questions" });
signature!(graph_outline, "kvasir.methods.graph.signatures.Outline", include_str!("prompts/graph_outline.md"),
    in { brief: String = "",
         questions: String = "one question per line: [id] name: question (status, claims found); what is missing" },
    out { sections: Vec<Section> = "three to seven sections" });
signature!(intake, "kvasir.research.signatures.Intake", include_str!("prompts/intake.md"),
    in { brief: String = "", question: String = "what the search that found the page was for",
         title: String = "", text: String = "a piece of the page" },
    out { findings: Vec<Finding> = "" });
signature!(check, "kvasir.research.evaluate.Check", include_str!("prompts/check.md"),
    in { question: String = "", report: String = "", requirement: String = "" },
    out { met: bool = "", reason: String = "one short sentence" });
signature!(ask_question, "kvasir.storm.storm_wiki.modules.knowledge_curation.AskQuestion",
    include_str!("prompts/ask_question.md"),
    in { topic: String = "topic you want to write about", conv: String = "conversation history" },
    out { reasoning: String = "", question: String = "a single question, or the closing thanks" });

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
