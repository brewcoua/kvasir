//! Verification past the quote check: the grounding checker, corroboration, contradiction.

mod common;

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use common::{Fetched, Tiers, calls, claim, ctx, events, page, quoting, target, tools};
use kvasir_fetch::domain;
use kvasir_research::claims::Claim;
use kvasir_research::corpus::Corpus;
use kvasir_research::corroborate::{copies, cross_check, neighbours, registrable};
use kvasir_research::ground::{grounded, prompt};
use kvasir_research::read::{self, GAP};
use kvasir_research::verify::{self, UNVERIFIED, Verified};
use kvasir_research::{Hit, Tools};
use kvasir_testkit::{ScriptedLlm, reply};
use kvasir_types::results::VerifyStage;
use kvasir_types::sites::Tier;

fn answer(verdict: &str, reason: &str, rewrite: &str) -> String {
    reply(&[("verdict", verdict), ("reason", reason), ("rewrite", rewrite)])
}

fn found(findings: &str) -> String {
    reply(&[("findings", findings)])
}

const NONSENSE: &str = "[[ ## nonsense ## ]]\nx\n\n[[ ## completed ## ]]";

/// What each checker call was shown.
fn asked(llm: &ScriptedLlm, role: &str) -> Vec<String> {
    let sent = llm.sent().into_iter().filter(|sent| sent.role == role);
    sent.map(|sent| sent.messages[0].content.clone()).collect()
}

fn by_id(claims: &[Claim]) -> HashMap<u64, Claim> {
    claims.iter().map(|claim| (claim.id, claim.clone())).collect()
}

#[tokio::test]
async fn the_checker_answers_yes_or_no() {
    for (answer, supported) in [("Yes", true), (" yes.", true), ("No", false)] {
        let llm = Arc::new(ScriptedLlm::replying(&[answer]));
        let tools = tools(&llm, &[]);

        let document = "It was found in 1904.";
        let claim = "The stone was found in 1904.";
        let decided = grounded(&tools, &ctx(), &target("ground"), document, claim).await.unwrap();

        assert_eq!(decided, supported);
        assert_eq!(asked(&llm, "ground"), [prompt(document, claim)]);
    }
}

fn hit(url: &str, title: &str) -> Hit {
    Hit { url: url.into(), title: title.into(), ..Hit::default() }
}

/// Tools whose pages all hold `text`.
fn reading(llm: &Arc<ScriptedLlm>, text: &str) -> Tools {
    let mut tools = tools(llm, &["intake", "ground_intake"]);
    tools.pages = Arc::new(Fetched { any: Some(page(text)), ..Fetched::default() });
    tools
}

async fn read_hits(tools: &Tools, hits: &[Hit], first_id: u64) -> Vec<Claim> {
    let mut corpus = Corpus::default();
    let ctx = ctx();
    let stage = kvasir_run::stages::RESEARCH;
    read::read(tools, &ctx, &mut corpus, "brief", "q", hits, first_id, 1, "search 1", None, stage)
        .await
        .unwrap()
}

#[tokio::test]
async fn intake_drops_a_claim_its_piece_does_not_support() {
    let piece = "The stone was found in 1904. It weighs 900 kg.";
    let findings = found(
        r#"[{"claim": "Found in 1904", "quote": "found in 1904", "confidence": "high"},
            {"claim": "Weighs a tonne", "quote": "It weighs 900 kg", "confidence": "high"}]"#,
    );
    let llm = Arc::new(ScriptedLlm::by_role(&[
        ("intake", &[&findings]),
        ("ground_intake", &["Yes", "No"]),
    ]));

    let claims = read_hits(&reading(&llm, piece), &[hit("https://a", "A")], 1).await;

    assert_eq!(claims.iter().map(|c| c.text.as_str()).collect::<Vec<_>>(), ["Found in 1904"]);
    // The whole piece is the document, since a claim names what its quote leaves to the page.
    assert_eq!(asked(&llm, "ground_intake")[0], prompt(piece, "Found in 1904"));
}

#[tokio::test]
async fn reading_grounds_the_claims_once_every_page_is_read() {
    let findings = found(r#"[{"claim": "Found", "quote": "found in 1904", "confidence": "high"}]"#);
    let llm = Arc::new(ScriptedLlm::by_role(&[
        ("intake", &[&findings, &findings]),
        ("ground_intake", &["Yes", "No"]),
    ]));
    let tools = reading(&llm, "It was found in 1904.");

    let claims = read_hits(&tools, &[hit("https://a", "A"), hit("https://b", "B")], 5).await;

    assert_eq!(calls(&llm), ["intake", "intake", "ground_intake", "ground_intake"]);
    let read: Vec<(u64, &str)> = claims.iter().map(|c| (c.id, c.url.as_str())).collect();
    assert_eq!(read, [(5, "https://a")]);
}

#[tokio::test]
async fn a_claim_is_grounded_on_the_piece_its_quote_is_in() {
    let (first, second) = ("The stone was found in 1904.", "It weighs 900 kg.");
    let findings =
        found(r#"[{"claim": "Weighs 900 kg", "quote": "It weighs 900 kg", "confidence": "high"}]"#);
    let llm =
        Arc::new(ScriptedLlm::by_role(&[("intake", &[&findings]), ("ground_intake", &["Yes"])]));
    let llm = Arc::new(Arc::into_inner(llm).unwrap().with_vectors(&[], Some(&[1.0, 0.0])));
    let tools = reading(&llm, "");
    // Two pieces of one page, recalled together, are read as one text split by the gap.
    let mut corpus = Corpus::default();
    let pieces = vec![first.to_owned(), second.to_owned()];
    corpus.add(Arc::new(page("x")), pieces, vec![vec![1.0, 0.0]; 2]).await;

    let stage = kvasir_run::stages::RESEARCH;
    let ctx = ctx();
    let claims =
        read::recall(&tools, &ctx, &mut corpus, "b", "q", 1, 1, "recall", None, None, stage, None)
            .await
            .unwrap();

    assert_eq!(claims.iter().map(|c| c.text.as_str()).collect::<Vec<_>>(), ["Weighs 900 kg"]);
    assert!(llm.sent()[0].messages[1].content.contains(&format!("{first}{GAP}{second}")));
    assert_eq!(asked(&llm, "ground_intake"), [prompt(second, "Weighs 900 kg")]);
}

async fn report(
    llm: &Arc<ScriptedLlm>,
    roles: &[&'static str],
    sections: &[(&str, &str)],
    claims: &[Claim],
) -> Verified {
    let tools = tools(llm, roles);
    let sections: Vec<(String, String)> =
        sections.iter().map(|(t, s)| ((*t).to_owned(), (*s).to_owned())).collect();
    verify::report(&tools, &ctx(), &sections, &by_id(claims)).await.unwrap()
}

#[tokio::test]
async fn the_checker_decides_every_cited_sentence_against_all_it_cites() {
    let claims = [claim(1, "https://a"), claim(2, "https://b")];
    // The other models are never asked: the checker keeps both.
    let llm = Arc::new(ScriptedLlm::by_role(&[("ground", &["Yes", "Yes"])]));

    let roles = &["ground", "verify", "escalate"];
    let checked = report(&llm, roles, &[("S", "One [1]. Both [1][2].")], &claims).await;

    assert_eq!(checked.sections, [("S".to_owned(), "One [1]. Both [1][2].".to_owned())]);
    assert_eq!(
        asked(&llm, "ground"),
        [
            prompt("claim 1\nquote #1.", "One."),
            prompt("claim 1\nquote #1.\n\nclaim 2\nquote #2.", "Both."),
        ]
    );
}

/// The report of one section holding `sentence`, with each model answering as told, and the roles
/// called in order.
async fn run(
    sentence: &str,
    checker: &[&str],
    fast: &[&str],
    reason: &[&str],
    claims: Option<&[Claim]>,
) -> (Verified, Vec<&'static str>) {
    let llm = Arc::new(ScriptedLlm::by_role(&[
        ("ground", checker),
        ("verify", fast),
        ("escalate", reason),
    ]));
    let mut roles = vec!["verify", "escalate"];
    if !checker.is_empty() {
        roles.push("ground");
    }
    let held = [claim(1, "https://a")];
    let checked = report(&llm, &roles, &[("S", sentence)], claims.unwrap_or(&held)).await;
    (checked, calls(&llm))
}

fn section(text: &str) -> Vec<(String, String)> {
    vec![("S".to_owned(), text.to_owned())]
}

fn removed(checked: &Verified) -> Vec<(VerifyStage, &str)> {
    checked.removed.iter().map(|r| (r.stage, r.reason.as_str())).collect()
}

#[tokio::test]
async fn a_sentence_citing_no_known_claim_is_removed_without_a_call() {
    let (checked, log) = run("Invented [9].", &[], &[], &[], None).await;

    assert_eq!(checked.sections, section(""));
    assert_eq!(
        removed(&checked),
        [(VerifyStage::Citation, "cites no claim the research gathered")]
    );
    assert!(log.is_empty());
}

#[tokio::test]
async fn a_sentence_the_checker_supports_never_reaches_the_fast_model() {
    let (checked, log) = run("Fact [1].", &["Yes"], &[], &[], None).await;

    assert_eq!(checked.sections, section("Fact [1]."));
    assert_eq!(log, ["ground"]);
}

#[tokio::test]
async fn a_sentence_the_checker_and_the_fast_model_reject_is_removed() {
    let fast = answer("unsupported", "not in the quote", "");
    let (checked, log) = run("Fact [1].", &["No"], &[&fast], &[], None).await;

    assert_eq!(checked.sections, section(""));
    assert_eq!(removed(&checked), [(VerifyStage::Verify, "not in the quote")]);
    assert_eq!(log, ["ground", "verify"]);
}

#[tokio::test]
async fn a_sentence_the_fast_model_supports_against_the_checker_goes_to_the_reason_model() {
    let supported = answer("supported", "because", "");
    let (checked, log) = run("Fact [1].", &["No"], &[&supported], &[&supported], None).await;

    assert_eq!(checked.sections, section("Fact [1]."));
    assert_eq!(log, ["ground", "verify", "escalate"]);
}

#[tokio::test]
async fn the_reason_model_may_remove_a_sentence() {
    let fast = answer("partial", "because", "");
    let reason = answer("unsupported", "adds a date", "");
    let (checked, _) = run("Fact [1].", &["No"], &[&fast], &[&reason], None).await;

    assert_eq!(checked.sections, section(""));
    assert_eq!(removed(&checked), [(VerifyStage::Escalate, "adds a date")]);
}

#[tokio::test]
async fn a_partial_sentence_is_rewritten_when_the_rewrite_passes_the_checker() {
    let rewrite = "Fact [1].";
    let fast = answer("partial", "because", "");
    let reason = answer("partial", "the year is not quoted", rewrite);
    let (checked, log) = run("Fact in 1904 [1].", &["No", "Yes"], &[&fast], &[&reason], None).await;

    assert_eq!(checked.sections, section(rewrite));
    let revised: Vec<_> = checked
        .revised
        .iter()
        .map(|r| (r.before.as_str(), r.after.as_str(), r.reason.as_str()))
        .collect();
    assert_eq!(revised, [("Fact in 1904 [1].", rewrite, "the year is not quoted")]);
    assert!(checked.removed.is_empty());
    assert_eq!(log, ["ground", "verify", "escalate", "ground"]);
}

#[tokio::test]
async fn a_rewrite_without_a_checker_is_checked_by_the_fast_model() {
    let fast = [answer("partial", "because", ""), answer("supported", "because", "")];
    let reason = answer("partial", "because", "**Fact** [1].");
    let (checked, log) = run("Fact [1].", &[], &[&fast[0], &fast[1]], &[&reason], None).await;

    assert_eq!(checked.sections, section("**Fact** [1]."));
    assert_eq!(log, ["verify", "escalate", "verify"]);
}

#[tokio::test]
async fn a_rewrite_the_checker_rejects_is_removed() {
    let fast = answer("partial", "because", "");
    let reason = answer("partial", "because", "Less [1].");
    let (checked, _) = run("Fact [1].", &["No", "No"], &[&fast], &[&reason], None).await;

    assert_eq!(checked.sections, section(""));
    assert_eq!(checked.removed.iter().map(|r| r.stage).collect::<Vec<_>>(), [VerifyStage::Rewrite]);
    assert!(checked.revised.is_empty());
}

#[tokio::test]
async fn a_rewrite_citing_a_new_claim_is_removed_unchecked() {
    let claims = [claim(1, "https://a"), claim(2, "https://b")];
    let fast = answer("partial", "because", "");
    let reason = answer("partial", "because", "Fact [1][2].");
    let (checked, log) = run("Fact [1].", &["No"], &[&fast], &[&reason], Some(&claims)).await;

    assert_eq!(checked.sections, section(""));
    assert_eq!(checked.removed.iter().map(|r| r.stage).collect::<Vec<_>>(), [VerifyStage::Rewrite]);
    assert_eq!(log, ["ground", "verify", "escalate"]);
}

#[tokio::test]
async fn a_rewrite_citing_nothing_is_removed_unchecked() {
    let fast = answer("partial", "because", "");
    let reason = answer("partial", "because", "Fact.");
    let (checked, log) = run("Fact [1].", &["No"], &[&fast], &[&reason], None).await;

    assert_eq!(checked.sections, section(""));
    assert_eq!(removed(&checked), [(VerifyStage::Rewrite, "the rewrite cites no claim")]);
    assert_eq!(log, ["ground", "verify", "escalate"]);
}

#[tokio::test]
async fn a_partial_verdict_without_a_rewrite_removes_the_sentence() {
    let partial = answer("partial", "because", "");
    let (checked, _) = run("Fact [1].", &[], &[&partial], &[&partial], None).await;

    assert_eq!(checked.removed.iter().map(|r| r.stage).collect::<Vec<_>>(), [VerifyStage::Rewrite]);
}

#[tokio::test]
async fn a_verdict_that_cannot_be_read_leaves_the_sentence_marked() {
    let fast = [answer("partial", "because", ""), answer("supported", "because", "")];
    let (checked, _) =
        run("Fact [1]. Other [1].", &[], &[&fast[0], &fast[1]], &[NONSENSE; 4], None).await;

    assert_eq!(checked.sections, section(&format!("Fact [1]. {UNVERIFIED} Other [1].")));
    let unverified: Vec<&str> = checked.unverified.iter().map(|u| u.sentence.as_str()).collect();
    assert_eq!(unverified, ["Fact [1]."]);
    assert!(checked.removed.is_empty());
}

#[tokio::test]
async fn a_sentence_left_unverified_still_counts_as_checked() {
    let fast = [answer("partial", "because", ""), answer("supported", "because", "")];
    let llm = Arc::new(ScriptedLlm::by_role(&[
        ("verify", &[&fast[0], &fast[1]]),
        ("escalate", &[NONSENSE; 4]),
    ]));
    let tools = tools(&llm, &["verify", "escalate"]);
    let ctx = ctx();

    let claims = by_id(&[claim(1, "https://a")]);
    verify::report(&tools, &ctx, &section("Fact [1]. Other [1]."), &claims).await.unwrap();

    let checked: Vec<String> = events(&ctx)
        .into_iter()
        .map(|event| event.detail)
        .filter(|detail| detail.starts_with("checked "))
        .collect();
    assert_eq!(checked.last().unwrap(), "checked 2 of 2 cited sentences");
}

#[tokio::test]
async fn an_unreadable_verdict_goes_to_the_reason_model() {
    let (checked, _) = run("A fact [1].", &[], &[NONSENSE; 4], &[NONSENSE; 4], None).await;

    assert_eq!(checked.sections, section(&format!("A fact [1]. {UNVERIFIED}")));
    assert!(checked.removed.is_empty());
}

#[tokio::test]
async fn without_a_checker_the_fast_model_keeps_a_supported_sentence_and_alone() {
    let supported = answer("supported", "because", "");
    let (checked, log) = run("Fact [1].", &[], &[&supported], &[], None).await;

    assert_eq!(checked.sections, section("Fact [1]."));
    assert_eq!(log, ["verify"]);
}

#[tokio::test]
async fn every_checker_call_comes_before_the_first_fast_call() {
    let unsupported = answer("unsupported", "because", "");
    let llm = Arc::new(ScriptedLlm::by_role(&[
        ("ground", &["No", "Yes", "No"]),
        ("verify", &[&unsupported, &unsupported]),
    ]));

    let sections = [("A", "One [1]. Two [1]."), ("B", "Three [1].")];
    let roles = &["ground", "verify", "escalate"];
    let checked = report(&llm, roles, &sections, &[claim(1, "https://a")]).await;

    assert_eq!(calls(&llm), ["ground", "ground", "ground", "verify", "verify"]);
    let expected = [("A", "Two [1]."), ("B", "")].map(|(t, s)| (t.to_owned(), s.to_owned()));
    assert_eq!(checked.sections, expected);
}

#[test]
fn the_unverified_mark_stays_with_its_sentence_and_is_not_a_claim() {
    let line = format!("One [1]. {UNVERIFIED} Two [2]. {UNVERIFIED} Three.");

    assert_eq!(
        verify::sentences(&line),
        [format!("One [1]. {UNVERIFIED}"), format!("Two [2]. {UNVERIFIED}"), "Three.".into()]
    );
    assert_eq!(verify::cited(&line), [1, 2]);
    assert_eq!(verify::unmarked(&format!("One [1]. {UNVERIFIED}")), "One.");
}

fn encoding(vectors: &[(&str, &[f32])]) -> Arc<ScriptedLlm> {
    Arc::new(ScriptedLlm::replying(&[]).with_vectors(vectors, None))
}

async fn pairs(claims: &[Claim], vectors: &[(&str, &[f32])]) -> Vec<(u64, u64)> {
    let tools = tools(&encoding(vectors), &[]);
    let found = neighbours(&tools.encoder, claims, &ctx()).await.unwrap();
    let mut ids: Vec<(u64, u64)> =
        found.into_iter().map(|(i, j)| (claims[i].id, claims[j].id)).collect();
    ids.sort_unstable();
    ids
}

#[tokio::test]
async fn neighbours_are_close_claims_from_other_sites() {
    let claims = [
        claim(1, "https://www.a.org/x"),
        claim(2, "https://a.org/y"),
        claim(3, "https://b.org"),
        claim(4, "https://c.org"),
    ];
    let vectors: &[(&str, &[f32])] = &[
        ("claim 1", &[1.0, 0.0]),
        ("claim 2", &[1.0, 0.0]),
        ("claim 3", &[0.9, 0.1]),
        ("claim 4", &[0.0, 1.0]),
    ];

    // The same site, once `www.` is dropped, is no corroboration; claim 4 is about another thing.
    assert_eq!(pairs(&claims, vectors).await, [(1, 3), (2, 3), (3, 1), (3, 2)]);
    assert_eq!(domain("https://www.A.org/x"), "a.org");
}

#[tokio::test]
async fn neighbours_are_not_subdomains_of_one_site_nor_a_copied_quote() {
    let a = quoting(1, "https://a.org", "The stone was found in the old quarry in 1904.");
    let claims = [
        a.clone(),
        quoting(2, "https://blog.a.org", "Another quote."),
        quoting(3, "https://b.org", "the stone was  FOUND in the old quarry in 1904. Old."),
        claim(4, "https://c.org"),
    ];
    let vectors: Vec<(String, [f32; 2])> =
        (1..=4).map(|i| (format!("claim {i}"), [1.0, 0.0])).collect();
    let vectors: Vec<(&str, &[f32])> = vectors.iter().map(|(t, v)| (t.as_str(), &v[..])).collect();

    assert_eq!(
        pairs(&claims, &vectors).await,
        [(1, 4), (2, 3), (2, 4), (3, 2), (3, 4), (4, 1), (4, 2), (4, 3)]
    );
    assert_eq!(registrable("https://a.org:8080/x"), "a.org");
    assert_eq!(registrable("https://blog.a.org"), "a.org");
    assert_eq!(registrable("http://127.0.0.1/x"), "127.0.0.1");
    assert_eq!(registrable("http://localhost/x"), "localhost");
    assert!(!copies(&quoting(5, "https://d.org", "in 1904"), &a));
    assert!(!copies(&quoting(6, "https://d.org", ""), &a));
}

const SAME: &[(&str, &[f32])] =
    &[("claim 1", &[1.0, 0.0]), ("claim 2", &[1.0, 0.0]), ("claim 3", &[1.0, 0.0])];

fn checking(replies: &[(&str, &[&str])]) -> (Arc<ScriptedLlm>, Tools) {
    let llm = Arc::new(ScriptedLlm::by_role(replies).with_vectors(SAME, None));
    let tools = tools(&llm, &["ground", "contradict", "verify"]);
    (llm, tools)
}

#[tokio::test]
async fn cross_checking_records_support_and_disagreement() {
    let disagree = reply(&[("disagree", "[2]")]);
    let (_, tools) = checking(&[("ground", &["No", "No"]), ("contradict", &[&disagree])]);
    let mut claims = [claim(1, "https://a.org"), claim(2, "https://b.org")];

    cross_check(&tools, &ctx(), &mut claims, true, true).await.unwrap();

    assert!(claims.iter().all(|c| c.corroborated_by.is_empty()));
    assert_eq!(claims[0].contradicted_by, [2]);
    assert_eq!(claims[1].contradicted_by, [1]);
}

#[tokio::test]
async fn claims_one_supports_are_not_asked_about_disagreeing() {
    // Claim 2 supports claim 1, not the other way.
    let (llm, tools) = checking(&[("ground", &["Yes", "No"])]);
    let mut claims = [claim(1, "https://a.org"), claim(2, "https://b.org")];

    cross_check(&tools, &ctx(), &mut claims, true, true).await.unwrap();

    assert_eq!(claims[0].corroborated_by, [2]);
    assert!(claims[1].corroborated_by.is_empty());
    assert!(claims.iter().all(|c| c.contradicted_by.is_empty()));
    assert!(!calls(&llm).contains(&"contradict"));
}

#[tokio::test]
async fn support_is_checked_for_every_pair_before_disagreement() {
    // Claim 1 is asked about 2 and 3 in one call, claim 2 about 3; 9 was never shown.
    let first = reply(&[("disagree", "[3, 9]")]);
    let second = reply(&[("disagree", "[]")]);
    let (llm, tools) = checking(&[("ground", &["No"; 6]), ("contradict", &[&first, &second])]);
    let mut claims =
        [claim(1, "https://a.org"), claim(2, "https://b.org"), claim(3, "https://c.org")];

    cross_check(&tools, &ctx(), &mut claims, true, true).await.unwrap();

    assert_eq!(calls(&llm), [vec!["ground"; 6], vec!["contradict"; 2]].concat());
    assert_eq!(claims[0].contradicted_by, [3]);
    assert!(claims[1].contradicted_by.is_empty());
    assert_eq!(claims[2].contradicted_by, [1]);
}

#[tokio::test]
async fn cross_checking_asks_nothing_unless_asked_to() {
    let (llm, tools) = checking(&[]);
    let mut claims = [claim(1, "https://a.org"), claim(2, "https://b.org")];

    cross_check(&tools, &ctx(), &mut claims, false, false).await.unwrap();

    assert!(claims.iter().all(|c| c.corroborated_by.is_empty()));
    assert!(llm.sent().is_empty());
}

#[tokio::test]
async fn a_sentence_whose_claims_all_come_from_low_sites_is_marked() {
    let claims =
        [claim(1, "https://low.org/1"), claim(2, "https://low.org/2"), claim(3, "https://a.org")];
    let tiers = Arc::new(Tiers::new(&[("low.org", Tier::Low)]));
    let corpus = Corpus::new(None, "m".into(), false, Some(tiers));
    let low = corpus.low_trust(&claims).await;
    let text = "Both low [1][2]. Mixed [1, 3]. Fine [3]. A transition. Unknown claim [9].";

    let found = verify::low_trust(&section(text), &by_id(&claims), &low);

    let marked: Vec<(&str, &str)> =
        found.iter().map(|m| (m.section.as_str(), m.sentence.as_str())).collect();
    assert_eq!(marked, [("S", "Both low [1][2].")]);
    assert_eq!(Corpus::default().low_trust(&claims).await, HashSet::new());
}
