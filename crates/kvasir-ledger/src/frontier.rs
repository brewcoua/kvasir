//! Which question to search for next, judged on what the research already holds.
//!
//! The default order is breadth first: least tried, then shallowest. The frontier orders questions
//! by a worth score computed in code instead, and stops searching a branch once it stops adding
//! anything. A branch is a first question and its follow-ups. A new claim that repeats one already
//! held is merged into it: it adds a source when it comes from another site, and nothing otherwise.
//!
//! The weights are not tuned. They are a first guess, to be kept or changed on evaluation.

use std::collections::hash_map::Entry;
use std::collections::{BTreeSet, HashMap, HashSet};

use kvasir_fetch::domain;
use kvasir_llm::{LlmError, Sink};
use kvasir_research::Encoder;
use kvasir_research::claims::Claim;
use kvasir_research::corroborate::independent;
use kvasir_research::encoder::cosine;
use kvasir_types::results::QuestionStatus;

use crate::questions::Question;
use crate::signatures::Priority;

/// Above this cosine similarity, a new claim repeats one already held.
const DUPLICATE_CLAIM: f64 = 0.92;
/// Searches in a row on one branch that add no claim and no source before the branch is closed.
pub const BRANCH_STALL: usize = 2;
/// What lowers a question's worth: each level below the first questions, and each query tried.
const DEPTH_PENALTY: f64 = 0.15;
const ATTEMPT_PENALTY: f64 = 0.25;
/// What raises it: a question with part of an answer, or with sources that disagree.
const GAP_BONUS: f64 = 0.2;

/// What the model that asked a question said of how central it is to the brief adds.
fn priority_bonus(priority: Priority) -> f64 {
    match priority {
        Priority::Core => 0.2,
        Priority::Supporting => 0.0,
        Priority::Peripheral => -0.2,
    }
}

/// Whether `question` is still to be searched for.
pub(crate) fn waiting(question: &Question, max_attempts: usize) -> bool {
    matches!(
        question.status,
        QuestionStatus::Open | QuestionStatus::Weak | QuestionStatus::Contradicted
    ) && question.attempts() < max_attempts
}

/// The worth of each question, the vectors of the run's claims, and the branches closed.
///
/// The run's claims are passed to each call, read as they grow; their vectors are encoded once
/// each, in claim order.
pub struct Frontier {
    brief: Vec<f32>,
    claim_vectors: Vec<Vec<f32>>,
    question_vectors: HashMap<u64, Vec<f32>>,
    stalled: HashMap<u64, usize>,
    /// The first questions whose branches are closed.
    pub closed: HashSet<u64>,
}

impl Frontier {
    pub async fn new(encoder: &Encoder, brief: &str, sink: &dyn Sink) -> Result<Self, LlmError> {
        Ok(Self {
            brief: encoder.one(brief, false, sink).await?,
            claim_vectors: Vec::new(),
            question_vectors: HashMap::new(),
            stalled: HashMap::new(),
            closed: HashSet::new(),
        })
    }

    /// The index of the question worth most of those not settled, outside a closed branch.
    pub async fn pick(
        &mut self,
        encoder: &Encoder,
        sink: &dyn Sink,
        questions: &[Question],
        claims: &[Claim],
        max_attempts: usize,
    ) -> Result<Option<usize>, LlmError> {
        let root_of = roots(questions);
        let mut best: Option<(f64, usize)> = None;
        for (i, question) in questions.iter().enumerate() {
            if !waiting(question, max_attempts) || self.closed.contains(&root_of[&question.id]) {
                continue;
            }
            let worth = self.worth(encoder, sink, question, claims).await?;
            // Of equal worth, the first asked.
            let better = best.is_none_or(|(most, at)| {
                worth > most || (worth == most && question.id < questions[at].id)
            });
            if better {
                best = Some((worth, i));
            }
        }
        Ok(best.map(|(_, i)| i))
    }

    /// Closeness to the brief, plus distance from every claim held, plus any gap and its
    /// priority, less depth and the queries already tried.
    pub async fn worth(
        &mut self,
        encoder: &Encoder,
        sink: &dyn Sink,
        question: &Question,
        claims: &[Claim],
    ) -> Result<f64, LlmError> {
        if let Entry::Vacant(entry) = self.question_vectors.entry(question.id) {
            entry.insert(encoder.one(&question.text, false, sink).await?);
        }
        self.held(encoder, sink, claims).await?;
        let vector = &self.question_vectors[&question.id];
        let relevance = cosine(vector, &self.brief);
        let novelty = if self.claim_vectors.is_empty() {
            1.0
        } else {
            1.0 - self.claim_vectors.iter().map(|v| cosine(vector, v)).fold(f64::MIN, f64::max)
        };
        let gap = if matches!(question.status, QuestionStatus::Weak | QuestionStatus::Contradicted)
        {
            GAP_BONUS
        } else {
            0.0
        };
        Ok(relevance + novelty + gap + priority_bonus(question.priority)
            - DEPTH_PENALTY * question.depth as f64
            - ATTEMPT_PENALTY * question.attempts() as f64)
    }

    /// Merges the last `new` claims of `claims`, which a search for the question at `index`
    /// found, and closes its branch if they added nothing. True when the branch was closed.
    pub async fn searched(
        &mut self,
        encoder: &Encoder,
        sink: &dyn Sink,
        questions: &[Question],
        index: usize,
        claims: &mut [Claim],
        new: usize,
    ) -> Result<bool, LlmError> {
        let root = roots(questions)[&questions[index].id];
        if self.merge(encoder, sink, claims, new).await? > 0 {
            self.stalled.insert(root, 0);
            return Ok(false);
        }
        let stalled = self.stalled.entry(root).or_insert(0);
        *stalled += 1;
        if *stalled < BRANCH_STALL {
            return Ok(false);
        }
        self.closed.insert(root);
        Ok(true)
    }

    /// How many of the last `new` claims of `claims` added a claim or a source. A repeat from
    /// another site is recorded as corroborating the claim it repeats.
    pub async fn merge(
        &mut self,
        encoder: &Encoder,
        sink: &dyn Sink,
        claims: &mut [Claim],
        new: usize,
    ) -> Result<usize, LlmError> {
        self.held(encoder, sink, claims).await?;
        let before = claims.len() - new;
        // Row i is claim i, so the closest row names the claim repeated.
        let mut held: Vec<&Vec<f32>> = self.claim_vectors[..before].iter().collect();
        let mut added = 0;
        for (offset, vector) in self.claim_vectors[before..].iter().enumerate() {
            let scores: Vec<f64> = held.iter().map(|v| cosine(vector, v)).collect();
            // The first of equal scores, as numpy's argmax takes it.
            let best = (0..scores.len()).reduce(|a, b| if scores[b] > scores[a] { b } else { a });
            if let Some(best) = best.filter(|&b| scores[b] >= DUPLICATE_CLAIM) {
                if independent(&claims[best], &claims[before + offset]) {
                    let id = claims[before + offset].id;
                    claims[best].corroborated_by.push(id);
                    added += 1;
                }
            } else {
                added += 1;
            }
            held.push(vector);
        }
        Ok(added)
    }

    /// Removes the claims read from `hosts`, as [`drop_claims`] does, with their vectors.
    pub fn drop<'a>(
        &mut self,
        hosts: &BTreeSet<String>,
        claims: &mut Vec<Claim>,
        questions: impl IntoIterator<Item = &'a mut Question>,
    ) -> HashSet<u64> {
        // Vectors are encoded in claim order, and may cover only the first claims.
        let vectors = std::mem::take(&mut self.claim_vectors);
        self.claim_vectors = vectors
            .into_iter()
            .zip(claims.iter())
            .filter(|(_, claim)| !hosts.contains(&domain(&claim.url)))
            .map(|(vector, _)| vector)
            .collect();
        drop_claims(hosts, claims, questions)
    }

    /// Every claim's vector, encoding only those not encoded before.
    pub async fn held(
        &mut self,
        encoder: &Encoder,
        sink: &dyn Sink,
        claims: &[Claim],
    ) -> Result<&[Vec<f32>], LlmError> {
        let missing: Vec<String> =
            claims[self.claim_vectors.len()..].iter().map(|c| c.text.clone()).collect();
        if !missing.is_empty() {
            self.claim_vectors.extend(encoder.encode(&missing, false, sink).await?);
        }
        Ok(&self.claim_vectors)
    }

    /// The vectors encoded so far, for a caller that has just called [`Frontier::held`].
    pub fn vectors(&self) -> &[Vec<f32>] {
        &self.claim_vectors
    }
}

/// Removes the claims read from `hosts`, their ids from every claim's `corroborated_by` and
/// `contradicted_by` and from the questions' answers, and returns the ids removed. A question left
/// with no claim is open again. Events already published keep the ids.
pub fn drop_claims<'a>(
    hosts: &BTreeSet<String>,
    claims: &mut Vec<Claim>,
    questions: impl IntoIterator<Item = &'a mut Question>,
) -> HashSet<u64> {
    let gone: HashSet<u64> =
        claims.iter().filter(|c| hosts.contains(&domain(&c.url))).map(|c| c.id).collect();
    if gone.is_empty() {
        return gone;
    }
    claims.retain(|claim| !gone.contains(&claim.id));
    for claim in claims.iter_mut() {
        claim.corroborated_by.retain(|id| !gone.contains(id));
        claim.contradicted_by.retain(|id| !gone.contains(id));
    }
    for question in questions {
        if question.claims.iter().any(|id| gone.contains(id)) {
            question.claims.retain(|id| !gone.contains(id));
            if question.claims.is_empty() {
                question.status = QuestionStatus::Open;
            }
        }
    }
    gone
}

/// Each question's first question, the one its follow-ups descend from.
pub fn roots(questions: &[Question]) -> HashMap<u64, u64> {
    let parents: HashMap<u64, Option<u64>> = questions.iter().map(|q| (q.id, q.parent)).collect();
    questions
        .iter()
        .map(|q| {
            let mut root = q.id;
            while let Some(&Some(parent)) = parents.get(&root) {
                root = parent;
            }
            (q.id, root)
        })
        .collect()
}
