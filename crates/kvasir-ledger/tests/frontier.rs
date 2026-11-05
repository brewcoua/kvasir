//! The frontier: the worth of each question, merging repeated claims, closing a spent branch.

#[path = "../../kvasir-research/tests/common/mod.rs"]
mod common;

use std::collections::BTreeSet;
use std::sync::Arc;

use common::{claim, ctx, quoting};
use kvasir_ledger::frontier::{BRANCH_STALL, Frontier};
use kvasir_ledger::questions::{MAX_QUESTIONS, Question, add_questions};
use kvasir_ledger::signatures::{NamedQuestion, Priority};
use kvasir_research::Encoder;
use kvasir_testkit::ScriptedLlm;
use kvasir_types::results::QuestionStatus;

fn encoder(vectors: &[(&str, &[f32])], default: Option<&[f32]>) -> Encoder {
    let llm = Arc::new(ScriptedLlm::replying(&[]).with_vectors(vectors, default));
    common::tools(&llm, &[]).encoder
}

#[tokio::test]
async fn a_repeat_from_another_site_adds_a_source_and_from_the_same_site_nothing() {
    let encoder = encoder(
        &[
            ("brief", &[1.0, 1.0]),
            ("claim 1", &[1.0, 0.0]),
            ("claim 2", &[1.0, 0.0]),
            ("claim 3", &[1.0, 0.0]),
            ("claim 4", &[0.0, 1.0]),
        ],
        None,
    );
    let ctx = ctx();
    let mut claims = vec![claim(1, "https://a.org/1")];
    let mut frontier = Frontier::new(&encoder, "brief", &ctx).await.unwrap();

    claims.push(claim(2, "https://www.a.org/2"));
    assert_eq!(frontier.merge(&encoder, &ctx, &mut claims, 1).await.unwrap(), 0);
    claims.extend([claim(3, "https://b.org"), claim(4, "https://c.org")]);
    assert_eq!(frontier.merge(&encoder, &ctx, &mut claims, 2).await.unwrap(), 2);
    assert_eq!(claims[0].corroborated_by, [3]);
}

#[tokio::test]
async fn a_repeat_from_a_subdomain_or_with_a_copied_quote_adds_no_source() {
    let encoder = encoder(
        &[
            ("brief", &[1.0, 1.0]),
            ("claim 1", &[1.0, 0.0]),
            ("claim 2", &[1.0, 0.0]),
            ("claim 3", &[1.0, 0.0]),
        ],
        None,
    );
    let ctx = ctx();
    let held = quoting(1, "https://a.org/1", "The stone was found in the old quarry in 1904.");
    let copied = "  the STONE was found in the\nold quarry in 1904. It is old. ";
    let mut claims =
        vec![held, claim(2, "https://blog.a.org/2"), quoting(3, "https://b.org", copied)];
    let mut frontier = Frontier::new(&encoder, "brief", &ctx).await.unwrap();

    assert_eq!(frontier.merge(&encoder, &ctx, &mut claims, 2).await.unwrap(), 0);
    assert!(claims[0].corroborated_by.is_empty());
    assert_eq!(claims.len(), 3);
}

#[tokio::test]
async fn a_branch_that_adds_nothing_is_closed_and_its_follow_ups_skipped() {
    let encoder = encoder(&[], Some(&[1.0, 0.0]));
    let ctx = ctx();
    let follow_up = Question { parent: Some(1), depth: 1, ..Question::new(2, "follow-up") };
    let questions = vec![Question::new(1, "first"), follow_up, Question::new(3, "other")];
    let mut frontier = Frontier::new(&encoder, "brief", &ctx).await.unwrap();

    for searches in 1..=BRANCH_STALL {
        let closed = frontier.searched(&encoder, &ctx, &questions, 1, &mut [], 0).await.unwrap();
        assert_eq!(closed, searches == BRANCH_STALL);
    }

    let picked = frontier.pick(&encoder, &ctx, &questions, &[], 4).await.unwrap();
    assert_eq!(picked, Some(2));
}

#[tokio::test]
async fn a_question_far_from_what_is_held_is_worth_more() {
    let encoder = encoder(
        &[
            ("brief", &[1.0, 1.0]),
            ("claim 1", &[1.0, 0.0]),
            ("known", &[1.0, 0.0]),
            ("unknown", &[0.0, 1.0]),
        ],
        Some(&[1.0, 1.0]),
    );
    let ctx = ctx();
    let claims = [claim(1, "https://a.org")];
    let questions = [Question::new(1, "known"), Question::new(2, "unknown")];
    let mut frontier = Frontier::new(&encoder, "brief", &ctx).await.unwrap();

    let picked = frontier.pick(&encoder, &ctx, &questions, &claims, 4).await.unwrap();
    assert_eq!(picked, Some(1));
}

#[tokio::test]
async fn depth_and_attempts_lower_the_worth() {
    let encoder = encoder(&[], Some(&[1.0, 0.0]));
    let ctx = ctx();
    let mut frontier = Frontier::new(&encoder, "brief", &ctx).await.unwrap();
    let shallow = Question::new(1, "a");
    let deep = Question { parent: Some(1), depth: 2, ..Question::new(2, "b") };
    let tried = Question { queries: vec!["q".into()], ..Question::new(3, "c") };

    let mut worth = async |q: &Question| frontier.worth(&encoder, &ctx, q, &[]).await.unwrap();
    let shallow = worth(&shallow).await;
    assert!(shallow > worth(&deep).await);
    assert!(shallow > worth(&tried).await);
}

#[tokio::test]
async fn a_more_central_question_is_worth_more() {
    let encoder = encoder(&[], Some(&[1.0, 0.0]));
    let ctx = ctx();
    let mut frontier = Frontier::new(&encoder, "brief", &ctx).await.unwrap();
    let core = Question { priority: Priority::Core, ..Question::new(1, "a") };
    let peripheral = Question { priority: Priority::Peripheral, ..Question::new(2, "b") };

    let core = frontier.worth(&encoder, &ctx, &core, &[]).await.unwrap();
    assert!(core > frontier.worth(&encoder, &ctx, &peripheral, &[]).await.unwrap());
}

#[tokio::test]
async fn a_follow_up_is_no_more_central_than_its_question() {
    let encoder = encoder(&[("a", &[1.0, 0.0]), ("b", &[0.0, 1.0])], None);
    let ctx = ctx();
    let named = |text: &str, priority| NamedQuestion {
        title: text.into(),
        question: text.into(),
        priority,
    };
    let mut questions = Vec::new();
    let first = [named("a", Priority::Peripheral)];
    add_questions(&encoder, &ctx, &mut questions, &first, None, MAX_QUESTIONS).await.unwrap();

    let follow_up = [named("b", Priority::Core)];
    add_questions(&encoder, &ctx, &mut questions, &follow_up, Some(0), MAX_QUESTIONS)
        .await
        .unwrap();

    let priorities: Vec<Priority> = questions.iter().map(|q| q.priority).collect();
    assert_eq!(priorities, [Priority::Peripheral, Priority::Peripheral]);
}

#[test]
fn a_priority_outside_the_three_fails_no_reply() {
    use kvasir_ledger::signatures::decompose;
    use kvasir_llm::Signature;
    use kvasir_llm::adapter::{self, Format};

    let reply = r#"{"questions": [{"title": "c", "question": "c", "priority": "high"}]}"#;
    let parsed = adapter::parse(Format::Json, &decompose::Sig::outputs(), reply).unwrap();
    let output: decompose::Output =
        serde_json::from_value(serde_json::Value::Object(parsed)).unwrap();

    assert_eq!(output.questions[0].priority, Priority::Supporting);
}

#[tokio::test]
async fn dropping_a_site_removes_its_claims_and_every_reference_to_them() {
    let encoder = encoder(&[], Some(&[1.0, 0.0]));
    let ctx = ctx();
    let mut claims =
        vec![claim(1, "https://a.org/1"), claim(2, "https://bad.org/2"), claim(3, "https://c.org")];
    claims[0].corroborated_by = vec![2, 3];
    claims[2].contradicted_by = vec![2];
    let answered = |id, claims: Vec<u64>| Question {
        status: QuestionStatus::Answered,
        claims,
        ..Question::new(id, "q")
    };
    let mut questions = [answered(1, vec![2, 3]), answered(2, vec![2])];
    let mut frontier = Frontier::new(&encoder, "brief", &ctx).await.unwrap();
    frontier.held(&encoder, &ctx, &claims).await.unwrap();

    let hosts = BTreeSet::from(["bad.org".to_owned()]);
    let gone = frontier.drop(&hosts, &mut claims, questions.iter_mut());

    assert_eq!(gone, [2].into());
    assert_eq!(claims.iter().map(|c| c.id).collect::<Vec<_>>(), [1, 3]);
    assert_eq!(frontier.held(&encoder, &ctx, &claims).await.unwrap().len(), 2);
    assert_eq!(claims[0].corroborated_by, [3]);
    assert!(claims[1].contradicted_by.is_empty());
    assert_eq!(
        (&questions[0].claims[..], questions[0].status),
        (&[3][..], QuestionStatus::Answered)
    );
    assert_eq!((&questions[1].claims[..], questions[1].status), (&[][..], QuestionStatus::Open));
}
