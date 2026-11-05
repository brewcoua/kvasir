//! The ledger signatures against `tests/golden/signatures.json`: each prompt byte for byte what
//! dspy sent, in both formats, and dspy's own replies parsed to what dspy parsed them to.

use kvasir_ledger::signatures::{
    assess, brief, check_answer, criteria, decompose, entities, outline, plan_queries,
    rank_criterion, search_for,
};
use kvasir_testkit::golden::{check, golden};

#[test]
fn prompts_and_parses_match_dspy() {
    let golden = golden();
    let mut failures = Vec::new();
    check::<brief::Sig>(&golden, &mut failures);
    check::<plan_queries::Sig>(&golden, &mut failures);
    check::<decompose::Sig>(&golden, &mut failures);
    check::<search_for::Sig>(&golden, &mut failures);
    check::<criteria::Sig>(&golden, &mut failures);
    check::<entities::Sig>(&golden, &mut failures);
    check::<check_answer::Sig>(&golden, &mut failures);
    check::<assess::Sig>(&golden, &mut failures);
    check::<rank_criterion::Sig>(&golden, &mut failures);
    check::<outline::Sig>(&golden, &mut failures);
    assert!(failures.is_empty(), "{}", failures.join("\n\n"));
}
