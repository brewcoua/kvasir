//! Which question to search next: the one that settles the most of the brief for the least work.
//!
//! This is proof-number search (Allis 1994) over the question graph. A question is settled by
//! searching it again, or by settling every question it raised: an OR of a retry and an AND of its
//! children. Its work is the fewest searches either way would take, as estimated here: none once
//! answered, one for a question never searched, one more than its attempts for a retry, since each
//! failed search makes the next less likely to succeed, and the sum of its children's work for the
//! children. Children that are all answered did not settle a question still judged unanswered, so
//! they are no way to settle it. A question in a closed branch never settles.
//!
//! An answered question is settled, so it counts as no work in its parent's split. Its own work, as
//! that of a question that can be neither retried nor split, is what is left below it: the work of
//! the questions it raised that can still be settled. So the run still reaches the follow-ups of an
//! answered question, and the children of a question given up.
//!
//! The run takes the first question with the most worth per unit of work, then walks down it to the
//! question whose search settles the most for the least: at each level, the question itself while a
//! retry costs less than its children, else its child with the least work.
//!
//! Two rules put the first questions ahead, since they are what the brief asks and what the report
//! is outlined from. A first question not yet answered after one search is searched again before
//! the run goes two levels below any first question. Late in the research, a first question that
//! can still be settled is searched before any question raised below one.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

use kvasir_ledger::frontier::{Frontier, roots};
use kvasir_ledger::questions::Question;
use kvasir_llm::{LlmError, Sink};
use kvasir_research::Encoder;
use kvasir_research::claims::Claim;
use kvasir_types::results::QuestionStatus;

pub const NEVER: f64 = f64::INFINITY;
/// Searches a first question not yet answered gets before the run goes two levels below any.
const FIRST_SEARCHES: usize = 2;

/// Each question's id, and the indices of the questions it raised.
pub fn children(questions: &[Question]) -> HashMap<u64, Vec<usize>> {
    let mut below: HashMap<u64, Vec<usize>> = questions.iter().map(|q| (q.id, vec![])).collect();
    for (i, question) in questions.iter().enumerate() {
        if let Some(parent) = question.parent {
            below.get_mut(&parent).expect("a parent in the graph").push(i);
        }
    }
    below
}

/// The work of searching `question` again.
fn retry(question: &Question, max_attempts: usize) -> f64 {
    if matches!(question.status, QuestionStatus::Answered | QuestionStatus::Unresolved)
        || question.attempts() >= max_attempts
    {
        return NEVER;
    }
    1.0 + question.attempts() as f64
}

/// The work of settling every question in `below`, the children of one question.
fn split(questions: &[Question], below: &[usize], of: &HashMap<u64, f64>) -> f64 {
    let answered = |i: &usize| questions[*i].status == QuestionStatus::Answered;
    if below.iter().all(answered) {
        return NEVER;
    }
    below.iter().map(|i| if answered(i) { 0.0 } else { of[&questions[*i].id] }).sum()
}

/// Each question's id, and the searches still needed to settle it.
pub fn work(
    questions: &[Question],
    max_attempts: usize,
    closed: &HashSet<u64>,
) -> HashMap<u64, f64> {
    let root_of = roots(questions);
    let below = children(questions);
    let mut of: HashMap<u64, f64> = HashMap::new();
    // A question is always added after the one that raised it, so children come first here.
    for question in questions.iter().rev() {
        if closed.contains(&root_of[&question.id]) {
            of.insert(question.id, NEVER);
            continue;
        }
        let left: f64 =
            below[&question.id].iter().map(|i| of[&questions[*i].id]).filter(|&w| w < NEVER).sum();
        if question.status == QuestionStatus::Answered {
            of.insert(question.id, left);
            continue;
        }
        let settle = retry(question, max_attempts).min(split(questions, &below[&question.id], &of));
        let work = if settle < NEVER {
            settle
        } else if left > 0.0 {
            left
        } else {
            NEVER
        };
        of.insert(question.id, work);
    }
    of
}

/// The index of the one of `among` with the greatest key; of equal keys, the first.
fn most<K: PartialOrd>(among: &[usize], key: impl Fn(usize) -> K) -> usize {
    let mut best = among[0];
    for &i in &among[1..] {
        if key(i).partial_cmp(&key(best)) == Some(Ordering::Greater) {
            best = i;
        }
    }
    best
}

/// The index of the question to search next, or None when no question can still be settled.
///
/// `late` when little of the research time is left.
pub async fn pick(
    frontier: &mut Frontier,
    encoder: &Encoder,
    sink: &dyn Sink,
    questions: &[Question],
    claims: &[Claim],
    max_attempts: usize,
    late: bool,
) -> Result<Option<usize>, LlmError> {
    let of = work(questions, max_attempts, &frontier.closed);
    let open = |i: &usize| {
        let work = of[&questions[*i].id];
        0.0 < work && work < NEVER
    };
    let below = children(questions);
    let first: Vec<usize> =
        (0..questions.len()).filter(|&i| questions[i].parent.is_none() && open(&i)).collect();
    if first.is_empty() {
        return Ok(None);
    }
    let mut worth: HashMap<usize, f64> = HashMap::new();
    for &i in &first {
        worth.insert(i, frontier.worth(encoder, sink, &questions[i], claims).await?);
    }
    let behind: Vec<usize> = first
        .iter()
        .copied()
        .filter(|&i| {
            let q = &questions[i];
            retry(q, max_attempts) < NEVER
                && (late || (0 < q.attempts() && q.attempts() < FIRST_SEARCHES))
        })
        .collect();
    let first_behind =
        |worth: &HashMap<usize, f64>| most(&behind, |i| (worth[&i], -(questions[i].id as i64)));
    if late && !behind.is_empty() {
        return Ok(Some(first_behind(&worth)));
    }
    let id = |i: usize| questions[i].id as f64;
    // Worth is positive for any question close to the brief; one far from it counts as none.
    let mut at = most(&first, |i| {
        let work = of[&questions[i].id];
        (worth[&i].max(0.0) / work, -work, -id(i))
    });
    loop {
        let question = &questions[at];
        let open_below: Vec<usize> = below[&question.id].iter().copied().filter(open).collect();
        // A tie goes to the children: they are what is left to find out about the question.
        if open_below.is_empty()
            || retry(question, max_attempts) < split(questions, &below[&question.id], &of)
        {
            return Ok(Some(at));
        }
        if question.depth >= 1 && !behind.is_empty() {
            return Ok(Some(first_behind(&worth)));
        }
        for &i in &open_below {
            worth.insert(i, frontier.worth(encoder, sink, &questions[i], claims).await?);
        }
        // The least work, then the most worth, then the first asked.
        at = most(&open_below, |i| (-of[&questions[i].id], worth[&i], -id(i)));
    }
}
