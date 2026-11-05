//! The signatures more than one research method uses. Each call sees one small input.

use kvasir_llm::shape::{Prop, exact};
use kvasir_llm::{choice, model, signature};
use kvasir_types::results::Confidence;
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Support {
    Supported,
    Partial,
    Unsupported,
}
const SUPPORT: &[&str] = &["supported", "partial", "unsupported"];
choice!(Support, SUPPORT, |v: &Value| exact(SUPPORT, v));

#[derive(Clone, Debug, Deserialize)]
pub struct Finding {
    pub claim: String,
    pub quote: String,
    pub confidence: Confidence,
}
model!(
    Finding,
    None,
    [
        Prop::of::<String>("claim"),
        Prop::of::<String>("quote"),
        // Models reach for "moderate" often enough that it fails whole replies.
        Prop::of::<Confidence>("confidence").lax(|v| {
            let v = if v == "moderate" { json!("medium") } else { v.clone() };
            exact(&["high", "medium", "low"], &v)
        }),
    ]
);

signature!(intake, "kvasir.research.signatures.Intake", include_str!("prompts/intake.md"),
    in { brief: String = "", question: String = "what the search that found the page was for",
         title: String = "", text: String = "a piece of the page" },
    out { findings: Vec<Finding> = "" });

signature!(write_section, "kvasir.research.signatures.WriteSection",
    include_str!("prompts/write_section.md"),
    in { brief: String = "", outline: String = "", section: String = "",
         subsections: String = "the `###` headings to use, one per line, or empty",
         earlier_sections: String = "the start of each section already written",
         evidence: String = "one claim per line, prefixed by its id in brackets" },
    out { text: String = "the section's paragraphs, in markdown" });

signature!(summarize, "kvasir.research.signatures.Summarize", include_str!("prompts/summarize.md"),
    in { brief: String = "", report: String = "the start of each section, with its claim ids" },
    out { points: Vec<String> = "three to five points, each citing claim ids" });

signature!(verify, "kvasir.research.signatures.Verify", include_str!("prompts/verify.md"),
    in { sentence: String = "",
         evidence: String = "each cited claim, with the quote it was read from" },
    out { verdict: Support = "", reason: String = "one short sentence" });

signature!(escalate, "kvasir.research.signatures.Escalate", include_str!("prompts/escalate.md"),
    in { sentence: String = "",
         evidence: String = "each cited claim, with the quote it was read from" },
    out { verdict: Support = "", reason: String = "one short sentence",
          rewrite: String = "the rewritten sentence if partial, otherwise empty" });

signature!(contradicts, "kvasir.research.signatures.Contradicts",
    include_str!("prompts/contradicts.md"),
    in { claim: String = "a claim, with the quote it was read from",
         others: String = "claims from other sources, each prefixed by its id in brackets, with its quote" },
    out { disagree: Vec<i64> = "the ids of the claims that disagree, or none" });

signature!(clarify, "kvasir.research.clarify.Clarify", include_str!("prompts/clarify.md"),
    in { question: String = "" },
    out { questions: Vec<String> = "at most three short questions, or an empty list" });
