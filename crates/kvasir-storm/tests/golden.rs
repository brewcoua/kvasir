//! The STORM signatures against `tests/golden/signatures.json`: each prompt byte for byte what
//! dspy sent, in both formats, and dspy's own replies parsed to what dspy parsed them to.

use kvasir_storm::signatures::{
    answer, ask, ask_as, draft_outline, lead, outline, personas, queries, related_topics, section,
};
use kvasir_testkit::golden::{check, golden};

#[test]
fn prompts_and_parses_match_dspy() {
    let golden = golden();
    let mut failures = Vec::new();
    check::<related_topics::Sig>(&golden, &mut failures);
    check::<personas::Sig>(&golden, &mut failures);
    check::<ask::Sig>(&golden, &mut failures);
    check::<ask_as::Sig>(&golden, &mut failures);
    check::<queries::Sig>(&golden, &mut failures);
    check::<answer::Sig>(&golden, &mut failures);
    check::<draft_outline::Sig>(&golden, &mut failures);
    check::<outline::Sig>(&golden, &mut failures);
    check::<section::Sig>(&golden, &mut failures);
    check::<lead::Sig>(&golden, &mut failures);
    assert!(failures.is_empty(), "{}", failures.join("\n\n"));
}
