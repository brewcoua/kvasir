//! The signatures only the graph method uses.

use kvasir_ledger::signatures::{Answer, NamedQuestion};
use kvasir_llm::shape::Prop;
use kvasir_llm::{model, signature};
use serde::Deserialize;
use serde_json::json;

/// One section of the report, and the questions whose findings it reports.
#[derive(Clone, Debug, Default, PartialEq, Deserialize)]
pub struct Section {
    pub title: String,
    pub questions: Vec<i64>,
    #[serde(default)]
    pub subsections: Vec<String>,
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

signature!(expand, "kvasir.methods.graph.signatures.Expand", include_str!("prompts/expand.md"),
    in { brief: String = "",
         path: String = "the questions above this one, first first, or none",
         question: String = "",
         evidence: String = "each claim with the quote it was read from" },
    out { status: Answer = "",
          claims: Vec<i64> = "ids of the claims counted as support",
          missing: String = "what is still missing, in a few words, or empty",
          raised: Vec<NamedQuestion> = "at most four questions, one when answered, or empty" });

signature!(gaps, "kvasir.methods.graph.signatures.Gaps", include_str!("prompts/gaps.md"),
    in { brief: String = "",
         questions: String = "one question per line: name: question (status)" },
    out { raised: Vec<NamedQuestion> = "at most three new questions, or empty" });

signature!(outline, "kvasir.methods.graph.signatures.Outline", include_str!("prompts/outline.md"),
    in { brief: String = "",
         questions: String = "one question per line: [id] name: question (status, claims found); what is missing" },
    out { sections: Vec<Section> = "three to seven sections" });
