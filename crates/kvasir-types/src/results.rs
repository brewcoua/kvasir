//! What a run produces, as its `done` event and its detail carry it.

use serde::{Deserialize, Serialize};

pub use crate::sites::{SourceInfo, Tier};

/// One source, numbered as the article's `[n]` markers reference it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Citation {
    pub index: u64,
    pub url: String,
    pub title: String,
    pub snippet: String,
    /// None when `[sources] enabled` is false, and in runs stored before it existed.
    #[serde(default)]
    pub source: Option<SourceInfo>,
}

/// Payload of the `done` event of a STORM run.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ArticleResult {
    pub article: String,
    pub outline: String,
    pub citations: Vec<Citation>,
    pub duration_seconds: f64,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Output {
    #[default]
    Report,
    Ranking,
    Table,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Confidence {
    High,
    Medium,
    Low,
}

/// One piece of evidence, with the verbatim quote it was read from.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ResearchClaim {
    pub id: u64,
    pub text: String,
    pub quote: String,
    pub url: String,
    pub title: String,
    pub confidence: Confidence,
    /// The search that found it. Defaulted, so a run stored when claims had rounds still loads.
    #[serde(default)]
    pub step: u64,
    /// Read from the search snippet because the page could not be fetched.
    #[serde(default)]
    pub from_snippet: bool,
    /// Claims from other sites that support this one, and that disagree with it.
    #[serde(default)]
    pub corroborated_by: Vec<u64>,
    #[serde(default)]
    pub contradicted_by: Vec<u64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum QuestionStatus {
    Open,
    Weak,
    Answered,
    Contradicted,
    Unresolved,
}

/// One question the research searched for, and how far it got.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ResearchQuestion {
    pub id: u64,
    pub text: String,
    /// A few words naming it, or empty when the model gave none.
    #[serde(default)]
    pub title: String,
    /// The question this one follows up on.
    pub parent: Option<u64>,
    pub depth: u64,
    pub status: QuestionStatus,
    /// The claims whose quotes support the answer.
    pub claims: Vec<u64>,
    pub queries: Vec<String>,
    /// What the last judgement found missing.
    pub missing: String,
}

/// The step of verification that decided a sentence.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VerifyStage {
    Citation,
    Checker,
    #[default]
    Verify,
    Escalate,
    Rewrite,
}

/// A sentence verification took out of the report, and why.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RemovedSentence {
    pub section: String,
    /// As written, so its `[n]` markers are claim ids rather than source numbers.
    pub sentence: String,
    pub reason: String,
    #[serde(default)]
    pub stage: VerifyStage,
}

/// A sentence verification found partly unsupported and rewrote to what its quotes say.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RevisedSentence {
    pub section: String,
    pub before: String,
    pub after: String,
    pub reason: String,
}

/// A sentence no step could judge, left in the report marked `[unverified]`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct UnverifiedSentence {
    pub section: String,
    pub sentence: String,
    pub reason: String,
}

/// A sentence whose cited claims all come from sites of low trust, left in the report.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct LowTrustSentence {
    pub section: String,
    pub sentence: String,
}

/// What a ranking or a table compares on, and how much it counts in the overall rank, 1 to 3.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Criterion {
    pub name: String,
    #[serde(default = "one")]
    pub weight: u8,
}

fn one() -> u8 {
    1
}

/// What the evidence says about one entity on one criterion.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Cell {
    pub criterion: String,
    /// Empty when no claim spoke to it.
    pub finding: String,
    pub claims: Vec<u64>,
    /// This entity's place on this criterion, averaged over both ranking passes. Rankings only.
    #[serde(default)]
    pub rank: Option<f64>,
}

/// One entity of a ranking or a table.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Row {
    pub entity: String,
    /// Overall place, from the weighted mean of its criterion ranks. Rankings only.
    #[serde(default)]
    pub rank: Option<u64>,
    pub cells: Vec<Cell>,
}

/// Payload of the `done` event of a research run.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ResearchResult {
    pub report: String,
    /// What the brief had to decide that the question and the clarifications left open.
    pub assumptions: Vec<String>,
    pub citations: Vec<Citation>,
    /// Every claim gathered, cited or not.
    pub claims: Vec<ResearchClaim>,
    #[serde(default)]
    pub questions: Vec<ResearchQuestion>,
    #[serde(default)]
    pub removed: Vec<RemovedSentence>,
    #[serde(default)]
    pub revised: Vec<RevisedSentence>,
    #[serde(default)]
    pub unverified: Vec<UnverifiedSentence>,
    #[serde(default)]
    pub low_trust: Vec<LowTrustSentence>,
    #[serde(default)]
    pub output: Output,
    #[serde(default)]
    pub criteria: Vec<Criterion>,
    /// Best first for a ranking. Empty for a report.
    #[serde(default)]
    pub matrix: Vec<Row>,
    pub duration_seconds: f64,
}

/// What a run can produce. Told apart by their fields, as pydantic's union was.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum RunResult {
    Article(ArticleResult),
    Research(ResearchResult),
}
