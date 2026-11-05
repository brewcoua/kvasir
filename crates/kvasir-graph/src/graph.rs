//! The graph a run builds: questions, the claims answering them, and the questions claims raise.
//!
//! Question nodes are the ledger's `Question`. A question's `parent` is the question whose evidence
//! raised it, and its `claims` the claims answering it. Claims corroborate or contradict each other
//! through their own `corroborated_by` and `contradicted_by`. A branch is a first question and every
//! question raised under it.

use indexmap::IndexMap;
use kvasir_ledger::frontier::roots;
use kvasir_ledger::questions::Question;

/// The questions `question` was raised under, the first question first.
pub fn path<'a>(questions: &'a [Question], question: &Question) -> Vec<&'a Question> {
    let mut above = Vec::new();
    let mut parent = question.parent;
    while let Some(id) = parent {
        let next = questions.iter().find(|q| q.id == id).expect("a parent in the graph");
        above.push(next);
        parent = next.parent;
    }
    above.reverse();
    above
}

/// Each first question's id, and the questions of its branch, itself first, in the order the
/// first questions were asked.
pub fn branches(questions: &[Question]) -> IndexMap<u64, Vec<&Question>> {
    let of = roots(questions);
    let mut grouped: IndexMap<u64, Vec<&Question>> = IndexMap::new();
    for question in questions {
        grouped.entry(of[&question.id]).or_default().push(question);
    }
    grouped
}
