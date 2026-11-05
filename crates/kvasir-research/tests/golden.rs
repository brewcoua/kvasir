//! The research signatures against `tests/golden/signatures.json`: each prompt byte for byte what
//! dspy sent, in both formats, and dspy's own replies parsed to what dspy parsed them to.

use kvasir_research::signatures::{
    clarify, contradicts, escalate, intake, summarize, verify, write_section,
};
use kvasir_testkit::golden::{check, golden};

#[test]
fn prompts_and_parses_match_dspy() {
    let golden = golden();
    let mut failures = Vec::new();
    check::<intake::Sig>(&golden, &mut failures);
    check::<write_section::Sig>(&golden, &mut failures);
    check::<summarize::Sig>(&golden, &mut failures);
    check::<verify::Sig>(&golden, &mut failures);
    check::<escalate::Sig>(&golden, &mut failures);
    check::<contradicts::Sig>(&golden, &mut failures);
    check::<clarify::Sig>(&golden, &mut failures);
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
