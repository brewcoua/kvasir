//! The ledger: the questions the research has to settle, and how far each got.

use kvasir_llm::{LlmError, Sink};
use kvasir_research::Encoder;
use kvasir_research::encoder::cosine;
use kvasir_types::results::{QuestionStatus, ResearchQuestion};

use crate::signatures::{NamedQuestion, Priority};

/// Questions in the ledger at most. Follow-ups past this are dropped: the budget could not search
/// for them anyway.
pub const MAX_QUESTIONS: usize = 24;
/// Above this cosine similarity, a new question is taken to repeat one in the ledger.
const DUPLICATE_QUESTION: f64 = 0.9;

/// One thing the research has to find out, and how far it got.
///
/// `Open`: nothing found yet. `Weak`: part of an answer. `Contradicted`: sources disagree.
/// `Unresolved`: every query shape was tried and found nothing.
#[derive(Clone, Debug, PartialEq)]
pub struct Question {
    pub id: u64,
    pub text: String,
    /// A few words naming it, when the model that asked it gave one.
    pub title: String,
    /// The question this one follows up on, one level shallower.
    pub parent: Option<u64>,
    pub depth: u64,
    pub status: QuestionStatus,
    pub priority: Priority,
    /// The claims whose quotes support the answer, as the last judgement found them.
    pub claims: Vec<u64>,
    pub queries: Vec<String>,
    /// What the last judgement found missing, for the next query to look for.
    pub missing: String,
    /// How many claims the run held at the last judgement, so a question is judged again only
    /// once there is something new to judge it on.
    pub judged_at: usize,
    /// For a ranking or a table: the option and the criterion of the empty cell this asks about.
    pub cell: Option<(String, String)>,
}

impl Question {
    pub fn new(id: u64, text: impl Into<String>) -> Self {
        Self {
            id,
            text: text.into(),
            title: String::new(),
            parent: None,
            depth: 0,
            status: QuestionStatus::Open,
            priority: Priority::Supporting,
            claims: Vec::new(),
            queries: Vec::new(),
            missing: String::new(),
            judged_at: 0,
            cell: None,
        }
    }

    pub fn attempts(&self) -> usize {
        self.queries.len()
    }
}

/// Adds to `ledger` those of `asked` that repeat no question in it, up to `limit`, as follow-ups
/// of the question at `parent` in it when given.
pub async fn add_questions(
    encoder: &Encoder,
    sink: &dyn Sink,
    ledger: &mut Vec<Question>,
    asked: &[NamedQuestion],
    parent: Option<usize>,
    limit: usize,
) -> Result<(), LlmError> {
    let asked: Vec<&NamedQuestion> =
        asked.iter().filter(|q| !q.question.trim().is_empty()).collect();
    if asked.is_empty() {
        return Ok(());
    }
    let texts: Vec<String> = asked.iter().map(|q| q.question.trim().to_owned()).collect();
    let mut all: Vec<String> = ledger.iter().map(|q| q.text.clone()).collect();
    all.extend(texts.iter().cloned());
    let mut vectors = encoder.encode(&all, false, sink).await?;
    let new = vectors.split_off(ledger.len());
    let mut kept = vectors;
    let parent = parent.map(|i| (ledger[i].id, ledger[i].depth, ledger[i].priority));
    for ((named, text), vector) in asked.into_iter().zip(texts).zip(new) {
        if ledger.len() >= limit {
            return Ok(());
        }
        let most = kept.iter().map(|k| cosine(&vector, k)).fold(f64::NEG_INFINITY, f64::max);
        if most > DUPLICATE_QUESTION {
            continue;
        }
        let mut question = Question::new(ledger.len() as u64 + 1, text);
        question.title = named.title.trim().to_owned();
        question.priority = named.priority;
        if let Some((id, depth, priority)) = parent {
            question.parent = Some(id);
            question.depth = depth + 1;
            // A follow-up is no more central than the question it follows up on.
            question.priority = question.priority.max(priority);
        }
        ledger.push(question);
        kept.push(vector);
    }
    Ok(())
}

/// The ledger as the outline reads it: each question, how far it got, and its claims.
pub fn notes(ledger: &[Question]) -> String {
    let lines: Vec<String> = ledger
        .iter()
        .filter(|q| q.cell.is_none())
        .map(|q| {
            let mut line = format!("- {} ({})", q.text, status_name(q.status));
            if !q.claims.is_empty() {
                line.push(' ');
                line.extend(q.claims.iter().map(|id| format!("[{id}]")));
            }
            if !q.missing.is_empty() {
                line.push_str(&format!("; missing: {}", q.missing));
            }
            line
        })
        .collect();
    lines.join("\n")
}

/// How the report names a question's status when the research did not settle it.
fn unsettled(status: QuestionStatus) -> Option<&'static str> {
    match status {
        QuestionStatus::Open | QuestionStatus::Unresolved => Some("not found"),
        QuestionStatus::Weak => Some("partly answered"),
        QuestionStatus::Contradicted => Some("sources disagree"),
        QuestionStatus::Answered => None,
    }
}

/// The questions the research did not settle, as a list for the end of the report.
///
/// Empty cells are left out: the table already shows them as a dash. So are follow-ups the budget
/// never reached, which were only suggestions; a first question it never reached says so.
pub fn open_questions(ledger: &[Question]) -> String {
    let lines: Vec<String> = ledger
        .iter()
        .filter(|q| q.cell.is_none() && (q.attempts() > 0 || q.depth == 0))
        .filter_map(|q| {
            let named = unsettled(q.status)?;
            let named = if q.attempts() > 0 { named } else { "not searched" };
            Some(format!("- {} ({named})", q.text))
        })
        .collect();
    lines.join("\n")
}

pub fn as_results(ledger: &[Question]) -> Vec<ResearchQuestion> {
    ledger
        .iter()
        .map(|q| ResearchQuestion {
            id: q.id,
            text: q.text.clone(),
            title: q.title.clone(),
            parent: q.parent,
            depth: q.depth,
            status: q.status,
            claims: q.claims.clone(),
            queries: q.queries.clone(),
            missing: q.missing.clone(),
        })
        .collect()
}

pub(crate) fn status_name(status: QuestionStatus) -> &'static str {
    match status {
        QuestionStatus::Open => "open",
        QuestionStatus::Weak => "weak",
        QuestionStatus::Answered => "answered",
        QuestionStatus::Contradicted => "contradicted",
        QuestionStatus::Unresolved => "unresolved",
    }
}
