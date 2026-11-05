//! The signatures only the question ledger uses. Each call sees one small input.

use kvasir_llm::shape::{Prop, exact};
use kvasir_llm::{choice, model, signature};
use kvasir_types::results::Output;
use serde::Deserialize;
use serde_json::{Value, json};

/// The shape of a report, as the brief chooses it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Form {
    Report,
    Ranking,
    Table,
}
const FORMS: &[&str] = &["report", "ranking", "table"];
choice!(Form, FORMS, |v: &Value| exact(FORMS, v));

impl From<Form> for Output {
    fn from(form: Form) -> Self {
        match form {
            Form::Report => Output::Report,
            Form::Ranking => Output::Ranking,
            Form::Table => Output::Table,
        }
    }
}

/// How central a question is to the brief, most central first.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Priority {
    Core,
    #[default]
    Supporting,
    Peripheral,
}
const PRIORITIES: &[&str] = &["core", "supporting", "peripheral"];
// A priority is only an ordering hint, so a word outside the three fails no reply.
choice!(Priority, PRIORITIES, |v: &Value| Some(
    exact(PRIORITIES, v).unwrap_or_else(|| "supporting".into())
));

/// A question, the short name a graph of the research shows it by, and how central it is.
#[derive(Clone, Debug, Default, PartialEq, Deserialize)]
pub struct NamedQuestion {
    pub title: String,
    pub question: String,
    pub priority: Priority,
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

#[derive(Clone, Debug, Deserialize)]
pub struct ChosenCriterion {
    pub name: String,
    pub weight: i64,
}
model!(ChosenCriterion, None, [Prop::of::<String>("name"), Prop::of::<i64>("weight")]);

/// How well the evidence answers a question, as the judge reads it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Answer {
    Answered,
    Weak,
    Contradicted,
    Open,
}
const ANSWERS: &[&str] = &["answered", "weak", "contradicted", "open"];
choice!(Answer, ANSWERS, |v: &Value| exact(ANSWERS, v));

signature!(brief, "kvasir.methods.ledger.signatures.Brief", include_str!("prompts/brief.md"),
    in { today: String = "today's date", question: String = "",
         clarifications: String = "questions asked and the answers given, or none" },
    out { brief: String = "one paragraph",
          assumptions: Vec<String> = "choices made on the author's behalf",
          output: Form = "ranking if the question asks which options are best, table if it asks to compare options without ranking them, report otherwise",
          criteria: Vec<String> = "the criteria the question says to compare or rank on, in its words, or empty",
          entities: Vec<String> = "the options the question names as the full set to compare, or empty if it leaves the set open" });

signature!(plan_queries, "kvasir.methods.ledger.signatures.PlanQueries",
    include_str!("prompts/plan_queries.md"),
    in { today: String = "today's date", brief: String = "",
         notes: String = "what is known so far, or empty at the start",
         count: i64 = "how many queries to return" },
    out { queries: Vec<String> = "search engine queries" });

signature!(decompose, "kvasir.methods.ledger.signatures.Decompose",
    include_str!("prompts/decompose.md"),
    in { brief: String = "" },
    out { questions: Vec<NamedQuestion> = "three to six questions" });

signature!(search_for, "kvasir.methods.ledger.signatures.SearchFor",
    include_str!("prompts/search_for.md"),
    in { today: String = "today's date", brief: String = "", question: String = "",
         tried: String = "queries already tried for this question, one per line",
         missing: String = "what the last search left out, or empty",
         shape: String = "the kind of query to write" },
    out { query: String = "a search engine query" });

signature!(criteria, "kvasir.methods.ledger.signatures.Criteria",
    include_str!("prompts/criteria.md"),
    in { brief: String = "", background: String = "search results on the topic" },
    out { criteria: Vec<ChosenCriterion> = "three to five criteria" });

signature!(entities, "kvasir.methods.ledger.signatures.Entities",
    include_str!("prompts/entities.md"),
    in { brief: String = "", background: String = "search results on the topic",
         count: i64 = "the most options to return" },
    out { entities: Vec<String> = "" });

signature!(check_answer, "kvasir.methods.ledger.signatures.CheckAnswer",
    include_str!("prompts/check_answer.md"),
    in { brief: String = "", question: String = "",
         evidence: String = "each claim with the quote it was read from" },
    out { status: Answer = "",
          claims: Vec<i64> = "ids of the claims counted as support",
          missing: String = "what is still missing, in a few words, or empty",
          follow_ups: Vec<String> = "at most two harder questions, or empty" });

signature!(assess, "kvasir.methods.ledger.signatures.Assess", include_str!("prompts/assess.md"),
    in { brief: String = "", entity: String = "the option", criterion: String = "",
         evidence: String = "one claim per line, prefixed by its id in brackets" },
    out { finding: String = "at most six words, or empty",
          claims: Vec<i64> = "ids of the claims the finding rests on" });

signature!(rank_criterion, "kvasir.methods.ledger.signatures.RankCriterion",
    include_str!("prompts/rank_criterion.md"),
    in { brief: String = "", criterion: String = "",
         options: String = "one option per line, with its finding" },
    out { ranking: Vec<String> = "every option's name, best first" });

signature!(outline, "kvasir.methods.ledger.signatures.Outline", include_str!("prompts/outline.md"),
    in { brief: String = "", shape: String = "how the report is organised", notes: String = "" },
    out { sections: Vec<String> = "section titles, three to seven" });
