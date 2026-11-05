//! The graph signatures against `tests/golden/signatures.json`: each prompt byte for byte what
//! dspy sent, in both formats, and dspy's own replies parsed to what dspy parsed them to.

use kvasir_graph::signatures::{expand, gaps, outline};
use kvasir_testkit::golden::{check, golden};

#[test]
fn prompts_and_parses_match_dspy() {
    let golden = golden();
    let mut failures = Vec::new();
    check::<expand::Sig>(&golden, &mut failures);
    check::<gaps::Sig>(&golden, &mut failures);
    check::<outline::Sig>(&golden, &mut failures);
    assert!(failures.is_empty(), "{}", failures.join("\n\n"));
}
