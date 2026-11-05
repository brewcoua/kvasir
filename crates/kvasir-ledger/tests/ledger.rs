//! The question ledger, with every model call, search and page faked.

#[path = "../../kvasir-research/tests/common/mod.rs"]
mod common;

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use common::{Fetched, Results, at, calls, claim, ctx, tools};
use kvasir_config::ResearchSettings;
use kvasir_ledger::brief::clarifications;
use kvasir_ledger::compare::{ordered, rank, verify_cells};
use kvasir_ledger::frontier::{BRANCH_STALL, Frontier};
use kvasir_ledger::questions::{MAX_QUESTIONS, Question, add_questions, open_questions};
use kvasir_ledger::repair::repair;
use kvasir_ledger::search_loop::{Judge, QUERY_SHAPES, Research, check, search_questions};
use kvasir_ledger::signatures::NamedQuestion;
use kvasir_ledger::{NO_EVIDENCE, research};
use kvasir_llm::BoxFuture;
use kvasir_research::claims::Claim;
use kvasir_research::corpus::Corpus;
use kvasir_research::verify::Verified;
use kvasir_research::{Hit, ResearchError, Search, Tools};
use kvasir_run::RunCtx;
use kvasir_testkit::{ScriptedLlm, reply};
use kvasir_types::requests::{Clarification, Input, LedgerOptions};
use kvasir_types::results::{
    Cell, Criterion, Output, QuestionStatus, RemovedSentence, ResearchResult, RevisedSentence, Row,
    UnverifiedSentence, VerifyStage,
};

const ROLES: &[&str] = &[
    "brief",
    "decompose",
    "plan",
    "check",
    "criteria",
    "entities",
    "assess",
    "judge",
    "outline",
    "tldr",
    "verify",
    "contradict",
    "escalate",
    "intake",
    "write",
];

/// A model replying to each role from its list, every text encoded alike, so nothing is closer
/// than anything else and none repeats.
fn scripted(replies: &[(&str, Vec<String>)]) -> Arc<ScriptedLlm> {
    let replies: Vec<(&str, Vec<&str>)> = replies
        .iter()
        .map(|(role, said)| (*role, said.iter().map(String::as_str).collect()))
        .collect();
    let replies: Vec<(&str, &[&str])> =
        replies.iter().map(|(role, said)| (*role, said.as_slice())).collect();
    Arc::new(ScriptedLlm::by_role(&replies).with_embedding(|_| vec![0.0]))
}

fn json(value: serde_json::Value) -> String {
    value.to_string()
}

fn framed(brief: &str, output: &str, criteria: &[&str], entities: &[&str]) -> String {
    reply(&[
        ("brief", brief),
        ("assumptions", "[]"),
        ("output", output),
        ("criteria", &json(criteria.into())),
        ("entities", &json(entities.into())),
    ])
}

fn split(questions: &[&str]) -> String {
    let named: Vec<_> =
        questions.iter().map(|q| serde_json::json!({"title": "t", "question": q})).collect();
    reply(&[("questions", &json(named.into()))])
}

fn query(query: &str) -> String {
    reply(&[("query", query)])
}

fn findings(found: &[(&str, &str)]) -> String {
    let found: Vec<_> = found
        .iter()
        .map(|(claim, quote)| serde_json::json!({"claim": claim, "quote": quote, "confidence": "high"}))
        .collect();
    reply(&[("findings", &json(found.into()))])
}

fn checked(status: &str, claims: &[u64]) -> String {
    reply(&[
        ("status", status),
        ("claims", &json(claims.into())),
        ("missing", ""),
        ("follow_ups", "[]"),
    ])
}

fn verdict(verdict: &str, reason: &str) -> String {
    reply(&[("verdict", verdict), ("reason", reason)])
}

fn hit(url: &str, title: &str, description: &str) -> Hit {
    Hit { url: url.into(), title: title.into(), description: description.into(), ..Hit::default() }
}

fn serving(llm: &Arc<ScriptedLlm>, hits: Vec<Hit>, pages: Fetched) -> Tools {
    let mut tools = tools(llm, ROLES);
    tools.search = Arc::new(Results::new(hits, Vec::new()));
    tools.pages = Arc::new(pages);
    tools
}

async fn run(
    tools: &Tools,
    ctx: &RunCtx,
    question: &str,
    options: LedgerOptions,
) -> Result<ResearchResult, ResearchError> {
    let input = Input { question: question.into(), clarifications: Vec::new() };
    research(tools, &ResearchSettings::default(), ctx, &input, &options, "2026-11-01").await
}

fn events(ctx: &RunCtx) -> Vec<(String, String)> {
    common::events(ctx).into_iter().map(|e| (e.stage, e.detail)).collect()
}

fn has(events: &[(String, String)], stage: &str, detail: &str) -> bool {
    events.iter().any(|(s, d)| s == stage && d == detail)
}

#[tokio::test]
async fn a_run_reads_pages_keeps_quoted_claims_and_cites_them() {
    let intake = findings(&[("Found in 1904", "found in   1904"), ("Made of gold", "solid gold")]);
    let llm = scripted(&[
        (
            "brief",
            vec![reply(&[
                ("brief", "When was the stone found?"),
                ("assumptions", r#"["any source"]"#),
                ("output", "report"),
                ("criteria", "[]"),
                ("entities", "[]"),
            ])],
        ),
        ("decompose", vec![split(&["When was the stone found?"])]),
        // The question's search, then the repair's four for the removed sentence, which find only
        // pages already read.
        (
            "plan",
            (0..5).map(|n| query(if n == 0 { "kvasir stone found" } else { "gold" })).collect(),
        ),
        // Neither quote is in the snippet page B falls back to.
        ("intake", vec![intake.clone(), intake]),
        ("check", vec![checked("answered", &[1, 7])]),
        ("outline", vec![reply(&[("sections", r#"["Findings"]"#)])]),
        ("write", vec![reply(&[("text", "It was found in 1904 [1][7]. It is made of gold [1].")])]),
        // No summary, so the report is the sections alone.
        ("tldr", vec![reply(&[("points", "[]")])]),
        (
            "verify",
            vec![
                verdict("supported", "the quote gives the year"),
                verdict("unsupported", "the quote says nothing about gold"),
            ],
        ),
    ]);
    let hits = vec![hit("https://a", "A", "snippet a"), hit("https://b", "B", "snippet b")];
    let page = at("https://a", "The stone\n\nIt was found in 1904 near Kvasir.");
    let tools = serving(&llm, hits, Fetched::of([page]));
    let ctx = ctx();

    let result = run(&tools, &ctx, "When was the Kvasir stone found?", LedgerOptions::default())
        .await
        .unwrap();

    // The paraphrased "solid gold" quote is on no page, so it is not evidence. The one question is
    // answered after one search, so the run asks for no other query.
    let claims: Vec<_> =
        result.claims.iter().map(|c| (c.id, c.text.as_str(), c.url.as_str())).collect();
    assert_eq!(claims, [(1, "Found in 1904", "https://a")]);
    assert_eq!(result.report, "## Findings\n\nIt was found in 1904 [1].");
    let removed: Vec<_> =
        result.removed.iter().map(|r| (r.sentence.as_str(), r.reason.as_str())).collect();
    assert_eq!(removed, [("It is made of gold [1].", "the quote says nothing about gold")]);
    let cited: Vec<_> =
        result.citations.iter().map(|c| (c.index, c.url.as_str(), c.snippet.as_str())).collect();
    assert_eq!(cited, [(1, "https://a", "found in   1904")]);
    assert_eq!(result.assumptions, ["any source"]);
    let questions: Vec<_> = result
        .questions
        .iter()
        .map(|q| (q.text.as_str(), q.status, q.claims.clone(), q.queries.clone()))
        .collect();
    let searched = vec!["kvasir stone found".to_owned()];
    assert_eq!(
        questions,
        [("When was the stone found?", QuestionStatus::Answered, vec![1], searched)]
    );

    let events = events(&ctx);
    let stages: Vec<&str> = events.iter().map(|(stage, _)| stage.as_str()).take(2).collect();
    assert_eq!(stages, ["brief", "research"]);
    // One event per intake call, each naming its page, the snippet fallback included.
    assert!(has(&events, "research", "search 1: read page 1/2: A"));
    assert!(has(&events, "research", "search 1: read page 2/2: B"));
    let result_line = "search 1: 1 new claims, 1 in all; When was the stone found?: answered";
    assert!(has(&events, "research", result_line));
    let fields = common::events(&ctx);
    let kinds: Vec<_> = fields.iter().map(|f| (f.kind.as_deref(), f.parent, f.page)).collect();
    let attempt = kinds.iter().position(|k| *k == (Some("attempt"), None, None)).unwrap() as u64;
    assert_eq!(fields[attempt as usize].question, Some(result.questions[0].id));
    assert!(kinds.contains(&(Some("page"), Some(attempt), Some(1))));
    assert!(kinds.contains(&(Some("page"), Some(attempt), Some(2))));
    assert!(kinds.contains(&(Some("result"), Some(attempt), None)));
    assert_eq!(kinds.iter().filter(|k| **k == (Some("split"), None, None)).count(), 1);
    assert_eq!(fields.iter().filter(|f| f.kind.as_deref() == Some("section")).count(), 1);
    assert!(has(&events, "verify", "removed 1 of 2 cited sentences"));
    let last = events.last().unwrap();
    assert_eq!(
        (last.0.as_str(), last.1.as_str()),
        ("verify", "found support for 0 of the sections they came from")
    );
    // The run's questions are recorded on it as they stand.
    assert_eq!(ctx.run.status(None).questions, result.questions);
}

/// A run whose one question finds a page with nothing on it.
fn nothing_found(brief: String, more: &[(&str, Vec<String>)]) -> Arc<ScriptedLlm> {
    let mut replies = vec![
        ("brief", vec![brief]),
        ("decompose", vec![split(&["q?"])]),
        ("intake", vec![findings(&[])]),
    ];
    replies.extend(more.iter().cloned());
    if !replies.iter().any(|(role, _)| *role == "plan") {
        replies.push(("plan", vec![query("q")]));
    }
    scripted(&replies)
}

fn nothing_relevant(llm: &Arc<ScriptedLlm>, description: &str) -> Tools {
    let hits = vec![hit("https://a", "A", description)];
    serving(llm, hits, Fetched::of([at("https://a", "nothing relevant")]))
}

#[tokio::test]
async fn a_run_with_no_evidence_writes_nothing() {
    // No outline or write replies: asking for either would exhaust the fake and fail the test.
    let llm = nothing_found(framed("b", "report", &[], &[]), &[]);
    let tools = nothing_relevant(&llm, "nothing");
    let ctx = ctx();

    let options = LedgerOptions { max_searches: Some(1), ..LedgerOptions::default() };
    let result = run(&tools, &ctx, "q", options).await.unwrap();

    assert_eq!(result.report, NO_EVIDENCE);
    assert!(result.citations.is_empty());
    let questions: Vec<_> = result.questions.iter().map(|q| (q.text.as_str(), q.status)).collect();
    assert_eq!(questions, [("q?", QuestionStatus::Open)]);
    let events = events(&ctx);
    let last = events.last().unwrap();
    assert_eq!(
        (last.0.as_str(), last.1.as_str()),
        ("write", "no evidence found, so nothing to write")
    );
}

#[test]
fn clarifications_reach_the_brief_as_text() {
    let asked = [Clarification { question: "Region?".into(), answer: "Europe".into() }];

    assert_eq!(clarifications(&asked), "Q: Region?\nA: Europe");
    assert_eq!(clarifications(&[]), "none");
}

#[tokio::test]
async fn a_ranking_run_ranks_each_criterion_twice_and_tables_the_cells() {
    let assessed = |finding: &str, claims: &[u64]| {
        reply(&[("finding", finding), ("claims", &json(claims.into()))])
    };
    let llm = scripted(&[
        // The question names the options, so they are not chosen again; a name given twice is kept
        // once, in its first spelling.
        ("brief", vec![framed("Which is fastest?", "report", &[], &["Alpha", "Beta", "alpha "])]),
        // The background searches, then the question's, then the empty cell's.
        (
            "plan",
            vec![
                reply(&[("queries", r#"["fast models"]"#)]),
                query("alpha beta speed"),
                query("alpha cost"),
            ],
        ),
        ("decompose", vec![split(&["How fast is each?"])]),
        // Out of range weights are clamped to 1 to 3.
        (
            "criteria",
            vec![reply(&[(
                "criteria",
                r#"[{"name": "speed", "weight": 3}, {"name": "cost", "weight": 0}]"#,
            )])],
        ),
        (
            "intake",
            vec![findings(&[
                ("Alpha: 50 tok/s", "Alpha runs at 50"),
                ("Beta: 20 tok/s", "Beta runs at 20"),
            ])],
        ),
        ("check", vec![checked("answered", &[1, 2])]),
        // Alpha on speed and cost, Beta on speed and cost. The search for Alpha's empty cost cell
        // finds only the page already read, so the cell is not assessed again.
        (
            "assess",
            vec![
                assessed("50 tok/s", &[1]),
                assessed("", &[]),
                // 9 does not exist, and 1 is about Alpha: neither is kept.
                assessed("20 tok/s", &[1, 2, 9]),
                assessed("cheap", &[2]),
            ],
        ),
        // Speed twice, the second pass reading the options in reverse. Beta's cost finding is
        // removed by verification, so no option has one and cost needs no judge.
        (
            "judge",
            vec![
                reply(&[("ranking", r#"["Alpha", "Beta"]"#)]),
                reply(&[("ranking", r#"["1. alpha"]"#)]),
            ],
        ),
        ("outline", vec![reply(&[("sections", r#"["Alpha"]"#)])]),
        ("write", vec![reply(&[("text", "Alpha is the fastest [1].")])]),
        ("tldr", vec![reply(&[("points", "[]")])]),
        // The three cells, then the sentence. Beta's cost is not in its quote.
        (
            "verify",
            vec![
                verdict("supported", "the quote gives the speed"),
                verdict("supported", "the quote gives the speed"),
                verdict("unsupported", "the quote says nothing of cost"),
                verdict("supported", "the quote gives the speed"),
            ],
        ),
    ]);
    let hits = vec![hit("https://a", "A", "speeds")];
    let page = at("https://a", "Alpha runs at 50 tok/s. Beta runs at 20 tok/s.");
    let tools = serving(&llm, hits, Fetched::of([page]));
    let ctx = ctx();

    let options = LedgerOptions {
        output: Some(Output::Ranking),
        max_attempts: Some(1),
        ..LedgerOptions::default()
    };
    let result = run(&tools, &ctx, "Which is faster, Alpha or Beta?", options).await.unwrap();

    assert_eq!(result.output, Output::Ranking);
    let criteria: Vec<_> = result.criteria.iter().map(|c| (c.name.as_str(), c.weight)).collect();
    assert_eq!(criteria, [("speed", 3), ("cost", 1)]);
    // Every cost ties at 1.5. Alpha: (3 x 1 + 1.5) / 4. Beta: (3 x 2 + 1.5) / 4.
    let ranks: Vec<_> = result.matrix.iter().map(|row| (row.entity.as_str(), row.rank)).collect();
    assert_eq!(ranks, [("Alpha", Some(1)), ("Beta", Some(2))]);
    let beta: Vec<_> = result.matrix[1]
        .cells
        .iter()
        .map(|c| (c.finding.as_str(), c.claims.clone(), c.rank))
        .collect();
    assert_eq!(beta, [("20 tok/s", vec![2], Some(2.0)), ("", vec![], Some(1.5))]);
    assert_eq!(result.matrix[0].cells[1].rank, Some(1.5));
    let removed: Vec<_> =
        result.removed.iter().map(|r| (r.section.as_str(), r.sentence.as_str(), r.stage)).collect();
    assert_eq!(removed, [("Ranking", "Beta, cost: cheap [2]", VerifyStage::Verify)]);
    // Both claims come from one page, so both cite source 1.
    assert!(
        result.report.starts_with(
            "## Ranking\n\n| Rank | Option | speed | cost |\n| --- | --- | --- | --- |\n\
         | 1 | Alpha | 50 tok/s [1] | – |\n\
         | 2 | Beta | 20 tok/s [1] | – |\n"
        ),
        "{}",
        result.report
    );
    assert!(result.report.ends_with("## Alpha\n\nAlpha is the fastest [1]."));
    // The page was read for the first question, so the search for the empty cell finds nothing.
    let events = events(&ctx);
    assert!(has(&events, "assess", "filled 0 of 1 empty cells"));
    assert!(has(&events, "assess", "ranked: Alpha, Beta"));
}

/// Two options with one cell each, `verify_cells` run over them, and what it left behind.
async fn check_cells(
    checker: &[&str],
    fast: &[String],
    reason: &[String],
) -> (Vec<Row>, Vec<RemovedSentence>, Vec<UnverifiedSentence>, Vec<&'static str>, RunCtx) {
    let fast: Vec<&str> = fast.iter().map(String::as_str).collect();
    let reason: Vec<&str> = reason.iter().map(String::as_str).collect();
    let llm = Arc::new(ScriptedLlm::by_role(&[
        ("ground", checker),
        ("verify", &fast),
        ("escalate", &reason),
    ]));
    let tools = tools(&llm, &["ground", "verify", "escalate"]);
    let cell =
        Cell { criterion: "speed".into(), finding: "50 tok/s".into(), claims: vec![1], rank: None };
    let mut rows: Vec<Row> = ["Alpha", "Beta"]
        .map(|name| Row { entity: name.into(), rank: None, cells: vec![cell.clone()] })
        .into();
    let by_id = HashMap::from([(1, claim(1, "https://a"))]);
    let ctx = ctx();
    let (removed, unverified) =
        verify_cells(&tools, &ctx, &mut rows, &by_id, "Ranking").await.unwrap();
    (rows, removed, unverified, calls(&llm), ctx)
}

fn findings_of(rows: &[Row]) -> Vec<&str> {
    rows.iter().map(|row| row.cells[0].finding.as_str()).collect()
}

#[tokio::test]
async fn a_cell_the_checker_supports_is_unchanged() {
    let (rows, removed, unverified, log, _) = check_cells(&["Yes", "Yes"], &[], &[]).await;

    assert_eq!(findings_of(&rows), ["50 tok/s", "50 tok/s"]);
    assert!(removed.is_empty() && unverified.is_empty());
    assert_eq!(log, ["ground", "ground"]);
}

#[tokio::test]
async fn a_cell_the_cascade_removes_is_emptied_and_ranks_last() {
    let fast = [verdict("unsupported", "no speed")];
    let (rows, removed, _, _, ctx) = check_cells(&["Yes", "No"], &fast, &[]).await;
    let llm = Arc::new(ScriptedLlm::replying(&[]));
    let tools = tools(&llm, &["judge"]);
    let criteria = [Criterion { name: "speed".into(), weight: 1 }];
    let ranked = rank(&tools, &ctx, "brief", &criteria, rows.clone()).await.unwrap();

    assert_eq!((rows[1].cells[0].finding.as_str(), rows[1].cells[0].claims.len()), ("", 0));
    let removed: Vec<_> =
        removed.iter().map(|r| (r.section.as_str(), r.sentence.as_str(), r.stage)).collect();
    assert_eq!(removed, [("Ranking", "Beta, speed: 50 tok/s [1]", VerifyStage::Verify)]);
    assert_eq!(ranked.iter().map(|row| row.entity.as_str()).collect::<Vec<_>>(), ["Alpha", "Beta"]);
    assert_eq!(ranked[1].cells[0].rank, Some(2.0));
}

#[tokio::test]
async fn a_partly_supported_cell_is_emptied_not_rewritten() {
    let fast = [verdict("partial", "r")];
    let reason = [reply(&[
        ("verdict", "partial"),
        ("reason", "the speed is rounded"),
        ("rewrite", "50 [1]"),
    ])];
    let (rows, removed, _, _, _) = check_cells(&["No", "Yes", "Yes"], &fast, &reason).await;

    assert_eq!(findings_of(&rows), ["", "50 tok/s"]);
    let removed: Vec<_> = removed.iter().map(|r| (r.stage, r.reason.as_str())).collect();
    let reason = "a partly supported cell is removed rather than rewritten (the speed is rounded)";
    assert_eq!(removed, [(VerifyStage::Rewrite, reason)]);
}

#[tokio::test]
async fn a_cell_no_step_can_judge_keeps_its_finding_marked() {
    let fast = [verdict("partial", "r")];
    // Unreadable in either format.
    let reason = [reply(&[("verdict", "x")]), reply(&[("verdict", "x")])];
    let (rows, _, unverified, _, _) = check_cells(&["No", "Yes"], &fast, &reason).await;

    assert_eq!(rows[0].cells[0].finding, "50 tok/s [unverified]");
    let unverified: Vec<_> =
        unverified.iter().map(|u| (u.section.as_str(), u.sentence.as_str())).collect();
    assert_eq!(unverified, [("Ranking", "Alpha, speed: 50 tok/s [1]")]);
}

#[tokio::test]
async fn every_cell_is_checked_before_the_fast_model_and_stays_on_the_assess_stage() {
    let fast = [verdict("unsupported", "r"), verdict("unsupported", "r")];
    let (_, _, _, log, ctx) = check_cells(&["No", "No"], &fast, &[]).await;

    assert_eq!(log, ["ground", "ground", "verify", "verify"]);
    let stages: HashSet<String> = events(&ctx).into_iter().map(|(stage, _)| stage).collect();
    assert_eq!(stages, HashSet::from(["assess".to_owned()]));
}

#[test]
fn a_ranked_name_matches_without_its_number_or_case() {
    let owned = |names: &[&str]| names.iter().map(|&n| n.to_owned()).collect::<Vec<_>>();
    let ranking = owned(&["2) beta", "Gamma", "BETA", "1. Alpha"]);

    assert_eq!(ordered(&ranking, &owned(&["Alpha", "Beta", "Delta"])), ["Beta", "Alpha", "Delta"]);
}

#[tokio::test]
async fn criteria_and_options_named_in_the_question_are_final() {
    // No criteria, entities or second plan reply: choosing either again would exhaust the fakes.
    // Galaxy is from the model's memory, not the question.
    let brief = framed(
        "Rank phones on price and privacy.",
        "ranking",
        &["price", "privacy"],
        &["Pixel", "Galaxy S30", "iPhone"],
    );
    let llm = nothing_found(brief, &[]);
    let tools = nothing_relevant(&llm, "nothing");
    let ctx = ctx();

    let options = LedgerOptions { max_searches: Some(1), ..LedgerOptions::default() };
    run(&tools, &ctx, "Rank the Pixel and the iPhone on price and privacy", options).await.unwrap();

    let background: Vec<_> =
        events(&ctx).into_iter().filter(|(stage, _)| stage == "background").collect();
    let only =
        ("background".to_owned(), "comparing 2 options on 2 criteria: Pixel, iPhone".to_owned());
    assert_eq!(background, [only]);
}

#[tokio::test]
async fn a_chosen_option_the_search_never_mentions_is_dropped() {
    let brief = framed("Rank phones on price.", "ranking", &["price"], &[]);
    // The background searches, then the question's.
    let plan = vec![reply(&[("queries", r#"["best phones"]"#)]), query("q")];
    let entities = vec![reply(&[("entities", r#"["Pixel 10", "Phantom 9"]"#)])];
    let llm = nothing_found(brief, &[("plan", plan), ("entities", entities)]);
    let tools = nothing_relevant(&llm, "The Pixel 10 leads");
    let ctx = ctx();

    let options = LedgerOptions { max_searches: Some(1), ..LedgerOptions::default() };
    run(&tools, &ctx, "Rank phones on price", options).await.unwrap();

    assert!(has(&events(&ctx), "background", "comparing 1 options on 1 criteria: Pixel 10"));
}

#[tokio::test]
async fn a_run_whose_searches_all_fail_stops_with_the_reason() {
    let llm = scripted(&[
        ("brief", vec![framed("b", "report", &[], &[])]),
        ("decompose", vec![split(&["q?"])]),
        ("plan", vec![query("q1"), query("q2"), query("q3")]),
    ]);
    let mut tools = tools(&llm, ROLES);
    let failures = vec!["brave: Suspended: too many requests".into(), "duckduckgo: CAPTCHA".into()];
    tools.search = Arc::new(Results::new(Vec::new(), failures));

    let failed = run(&tools, &ctx(), "q", LedgerOptions::default()).await.unwrap_err();

    // Each engine once, though every search reported it.
    assert_eq!(
        failed.to_string(),
        "3 searches in a row failed: brave: Suspended: too many requests; duckduckgo: CAPTCHA"
    );
}

/// SearXNG answering each query with one page never seen before, the first `failing` queries
/// failing instead.
#[derive(Default)]
struct Fresh {
    failing: usize,
    queries: Mutex<Vec<String>>,
}

impl Search for Fresh {
    fn search<'a>(
        &'a self,
        query: &'a str,
        _exclude: &'a HashSet<String>,
        _ctx: &'a RunCtx,
    ) -> BoxFuture<'a, (Vec<Hit>, Vec<String>)> {
        let mut queries = self.queries.lock().unwrap();
        queries.push(query.to_owned());
        let found = if queries.len() <= self.failing {
            (Vec::new(), vec!["brave: CAPTCHA".to_owned()])
        } else {
            let url = format!("https://example.org/{}", queries.len());
            (vec![hit(&url, &url, "")], Vec::new())
        };
        Box::pin(async move { found })
    }
}

/// A model writing `query N` for the Nth plan, finding on the page of a search the claim texts
/// `found` holds for the number of plans made by then, and judging each question as `judged`
/// says.
fn looping(found: HashMap<usize, Vec<&'static str>>, judged: &'static str) -> Arc<ScriptedLlm> {
    let plans = Mutex::new(0);
    let llm = ScriptedLlm::new(move |request| {
        let mut plans = plans.lock().unwrap();
        Ok(match request.target.role {
            "plan" => {
                *plans += 1;
                query(&format!("query {}", *plans))
            }
            "intake" => {
                let texts = found.get(&*plans).cloned().unwrap_or_default();
                let found: Vec<(&str, &str)> = texts.iter().map(|t| (*t, "piece")).collect();
                findings(&found)
            }
            "check" => reply(&[
                ("status", judged),
                ("claims", "[1, 2, 3, 4, 5, 6, 7, 8, 9]"),
                ("missing", ""),
                ("follow_ups", "[]"),
            ]),
            role => panic!("no reply for {role}"),
        })
    });
    Arc::new(llm.with_embedding(|_| vec![0.0]))
}

fn state<'a>(tools: &'a Tools, ctx: &'a RunCtx, max_attempts: usize) -> Research<'a> {
    Research {
        tools,
        ctx,
        today: "2026-11-01".into(),
        brief: "brief".into(),
        claims: Vec::new(),
        seen: HashSet::new(),
        corpus: Corpus::default(),
        per_query: 1,
        max_attempts,
        searched: 0,
        frontier: None,
        rows: Vec::new(),
    }
}

fn plans(llm: &ScriptedLlm) -> Vec<String> {
    llm.sent()
        .into_iter()
        .filter(|sent| sent.role == "plan")
        .map(|sent| {
            let prompt = &sent.messages.last().unwrap().content;
            let shape = prompt.split("[[ ## shape ## ]]\n").nth(1).unwrap();
            shape.split("\n\n").next().unwrap().to_owned()
        })
        .collect()
}

fn on(llm: &Arc<ScriptedLlm>, search: Fresh) -> (Tools, Arc<Fresh>) {
    let search = Arc::new(search);
    let mut tools = tools(llm, &["plan", "intake", "check"]);
    tools.search = search.clone();
    tools.pages = Arc::new(Fetched { any: Some(at("https://any", "piece")), ..Fetched::default() });
    (tools, search)
}

async fn search_all(
    run: &mut Research<'_>,
    questions: &mut Vec<Question>,
    budget: usize,
    judge: &Judge,
) -> usize {
    search_questions(run, questions, 0, budget, judge, "research", None, false).await.unwrap()
}

#[tokio::test]
async fn a_question_nothing_answers_is_tried_in_every_shape_then_given_up() {
    let llm = looping(HashMap::new(), "open");
    let (tools, _) = on(&llm, Fresh::default());
    let ctx = ctx();
    let mut run = state(&tools, &ctx, 4);
    let mut questions = vec![Question::new(1, "When?")];

    let searches = search_all(&mut run, &mut questions, 16, &Judge::Check).await;

    assert_eq!(searches, 4);
    assert_eq!(plans(&llm), QUERY_SHAPES);
    assert_eq!(questions[0].queries, ["query 1", "query 2", "query 3", "query 4"]);
    assert_eq!(questions[0].status, QuestionStatus::Unresolved);
}

#[tokio::test]
async fn the_budget_caps_the_searches() {
    let llm = looping(HashMap::new(), "open");
    let (tools, _) = on(&llm, Fresh::default());
    let ctx = ctx();
    let mut run = state(&tools, &ctx, 4);
    let mut questions = vec![Question::new(1, "a"), Question::new(2, "b")];

    let searches = search_all(&mut run, &mut questions, 3, &Judge::Check).await;

    assert_eq!(searches, 3);
    // Each gets a plain search before either gets a narrower one.
    assert_eq!(questions.iter().map(Question::attempts).collect::<Vec<_>>(), [2, 1]);
}

#[tokio::test]
async fn a_question_another_search_answered_costs_no_search() {
    let llm = looping(HashMap::from([(1, vec!["a and b"])]), "answered");
    let (tools, _) = on(&llm, Fresh::default());
    let ctx = ctx();
    let mut run = state(&tools, &ctx, 4);
    let mut questions = vec![Question::new(1, "a"), Question::new(2, "b")];

    let searches = search_all(&mut run, &mut questions, 16, &Judge::Check).await;

    assert_eq!(searches, 1);
    assert_eq!(calls(&llm).iter().filter(|role| **role == "check").count(), 2);
    assert!(questions[1].queries.is_empty());
    assert_eq!(questions[1].status, QuestionStatus::Answered);
}

#[tokio::test]
async fn a_failed_search_spends_the_budget_but_not_the_attempt() {
    let llm = looping(HashMap::new(), "open");
    let (tools, search) = on(&llm, Fresh { failing: 2, ..Fresh::default() });
    let ctx = ctx();
    let mut run = state(&tools, &ctx, 4);
    let mut questions = vec![Question::new(1, "When?")];

    let searches = search_all(&mut run, &mut questions, 3, &Judge::Check).await;

    assert_eq!(searches, 3);
    // Two failures, then the one search answered: the plain shape each time.
    assert_eq!(plans(&llm), [QUERY_SHAPES[0]; 3]);
    assert_eq!(questions[0].queries, ["query 3"]);
    assert_eq!(search.queries.lock().unwrap().len(), 3);
}

#[tokio::test]
async fn the_frontier_stops_searching_a_branch_that_adds_nothing() {
    let found = HashMap::from([(2, vec!["x"]), (4, vec!["y"]), (5, vec!["z"]), (6, vec!["w"])]);
    let llm = looping(found, "open");
    let (tools, _) = on(&llm, Fresh::default());
    let ctx = ctx();
    let mut run = state(&tools, &ctx, 4);
    run.frontier = Some(Frontier::new(&tools.encoder, "brief", &ctx).await.unwrap());
    let mut questions = vec![Question::new(1, "a"), Question::new(2, "b")];

    let searches = search_all(&mut run, &mut questions, 16, &Judge::Check).await;

    // Breadth first, the first question would get every query shape before the run gave up on it.
    assert_eq!(questions[0].attempts(), BRANCH_STALL);
    assert_eq!(questions[1].attempts(), 4);
    assert_eq!(searches, BRANCH_STALL + 4);
}

#[tokio::test]
async fn a_site_query_stands_in_for_the_planned_query_of_a_later_attempt() {
    let llm = looping(HashMap::new(), "open");
    let (tools, _) = on(&llm, Fresh::default());
    let ctx = ctx();
    let mut run = state(&tools, &ctx, 2);
    // Two claims of a site, both confirmed elsewhere: as high an affinity as a site gets.
    run.claims = (1..=2)
        .map(|id| Claim {
            corroborated_by: vec![99],
            ..claim(id, &format!("https://example.org/{id}"))
        })
        .collect();
    run.corpus.pages.insert("example.org".into(), 2);
    let mut questions = vec![Question { judged_at: 2, ..Question::new(1, "a") }];

    search_all(&mut run, &mut questions, 16, &Judge::Check).await;

    // The first attempt is planned; the second needs no planner call.
    assert_eq!(questions[0].queries, ["query 1", "site:example.org a"]);
    assert_eq!(plans(&llm).len(), 1);
}

#[tokio::test]
async fn the_judge_counts_only_claims_it_was_shown() {
    let answer = reply(&[
        ("status", "answered"),
        ("claims", "[2, 9]"),
        ("missing", ""),
        ("follow_ups", r#"["Why?"]"#),
    ]);
    let llm = scripted(&[("check", vec![answer])]);
    let tools = tools(&llm, &["check"]);
    let claims = [claim(1, "https://a"), claim(2, "https://b")];
    let mut question = Question::new(1, "When?");

    let asked = check(&tools, &ctx(), "brief", &claims, &mut question).await.unwrap();

    assert_eq!(
        (question.status, &question.claims[..], question.judged_at),
        (QuestionStatus::Answered, &[2][..], 2)
    );
    assert_eq!(asked, ["Why?"]);
    // Nothing new among the closest claims: no call, which would exhaust the fake.
    assert!(check(&tools, &ctx(), "brief", &claims, &mut question).await.unwrap().is_empty());
}

#[tokio::test]
async fn an_answer_resting_on_no_shown_claim_is_open() {
    let answer = reply(&[
        ("status", "answered"),
        ("claims", "[9]"),
        ("missing", "the year"),
        ("follow_ups", r#"["Why?"]"#),
    ]);
    let llm = scripted(&[("check", vec![answer])]);
    let tools = tools(&llm, &["check"]);
    let mut question = Question::new(1, "When?");

    let asked =
        check(&tools, &ctx(), "brief", &[claim(1, "https://a")], &mut question).await.unwrap();

    assert_eq!(
        (question.status, question.claims.len(), question.missing.as_str()),
        (QuestionStatus::Open, 0, "the year")
    );
    assert!(asked.is_empty());
}

#[tokio::test]
async fn a_repeated_question_is_not_added() {
    let llm = Arc::new(ScriptedLlm::replying(&[]).with_embedding(|text| {
        if text.contains("year") { vec![1.0, 0.0] } else { vec![0.0, 1.0] }
    }));
    let encoder = tools(&llm, &[]).encoder;
    let mut ledger = vec![Question::new(1, "Which year?")];
    let asked: Vec<NamedQuestion> = ["The year it was found?", "Who found it?", " "]
        .map(|q| NamedQuestion {
            title: "t".into(),
            question: q.into(),
            ..NamedQuestion::default()
        })
        .into();

    add_questions(&encoder, &ctx(), &mut ledger, &asked, Some(0), MAX_QUESTIONS).await.unwrap();

    let added: Vec<_> = ledger.iter().map(|q| (q.id, q.text.as_str(), q.parent, q.depth)).collect();
    assert_eq!(added, [(1, "Which year?", None, 0), (2, "Who found it?", Some(1), 1)]);
}

#[test]
fn the_report_ends_with_what_the_research_did_not_settle() {
    let searched = vec!["q".to_owned()];
    let question = |id, text: &str, status| Question {
        status,
        queries: searched.clone(),
        ..Question::new(id, text)
    };
    let ledger = [
        question(1, "When?", QuestionStatus::Answered),
        question(2, "Who?", QuestionStatus::Unresolved),
        question(3, "Why?", QuestionStatus::Weak),
        question(4, "Where?", QuestionStatus::Contradicted),
        Question {
            cell: Some(("Alpha".into(), "cost".into())),
            ..Question::new(5, "cost of Alpha")
        },
        Question::new(6, "How?"),
        // A follow-up the budget never reached.
        Question { parent: Some(1), depth: 1, ..Question::new(7, "How exactly?") },
    ];

    assert_eq!(
        open_questions(&ledger),
        "- Who? (not found)\n- Why? (partly answered)\n- Where? (sources disagree)\n\
         - How? (not searched)"
    );
}

#[tokio::test]
async fn a_section_whose_removed_sentence_the_search_supports_is_written_again() {
    let llm = scripted(&[
        ("plan", vec![query("gold")]),
        ("intake", vec![findings(&[("It is gold", "solid gold")])]),
        ("check", vec![checked("answered", &[2])]),
        ("write", vec![reply(&[("text", "It is gold [2]. It shines [2].")])]),
        ("verify", vec![verdict("supported", "says gold"), verdict("unsupported", "no shine")]),
    ]);
    let tools = serving(
        &llm,
        vec![hit("https://b", "B", "")],
        Fetched::of([at("https://b", "It is solid gold.")]),
    );
    let ctx = ctx();
    let mut run = state(&tools, &ctx, 4);
    run.claims = vec![claim(1, "https://a")];
    let removed = vec![
        RemovedSentence {
            section: "Findings".into(),
            sentence: "It is gold [1].".into(),
            reason: "no gold".into(),
            stage: VerifyStage::Verify,
        },
        RemovedSentence {
            section: "Ranking".into(),
            sentence: "Row [1].".into(),
            reason: "never asked".into(),
            stage: VerifyStage::Verify,
        },
    ];
    let verified = Verified {
        sections: vec![
            ("Ranking".into(), "| table |".into()),
            ("Findings".into(), "Found in 1904 [1].".into()),
        ],
        removed,
        // Stale once the section is written again.
        revised: vec![RevisedSentence {
            section: "Findings".into(),
            before: "a [1].".into(),
            after: "b [1].".into(),
            reason: "r".into(),
        }],
        unverified: vec![UnverifiedSentence {
            section: "Findings".into(),
            sentence: "c [1].".into(),
            reason: "r".into(),
        }],
    };

    let titles = ["Findings".to_owned()];
    let checked = repair(&mut run, &mut Vec::new(), &titles, 5, verified).await.unwrap();

    // Only sentences from written sections: the ranking table is built, not written.
    let asked = &llm.sent().into_iter().find(|sent| sent.role == "plan").unwrap().messages;
    let prompt = &asked.last().unwrap().content;
    assert!(prompt.contains("[[ ## question ## ]]\nIt is gold.\n"), "{prompt}");
    assert!(prompt.contains("[[ ## missing ## ]]\nno gold\n"), "{prompt}");
    let sections: Vec<(&str, &str)> =
        checked.sections.iter().map(|(t, s)| (t.as_str(), s.as_str())).collect();
    assert_eq!(sections, [("Ranking", "| table |"), ("Findings", "It is gold [2].")]);
    assert!(checked.revised.is_empty() && checked.unverified.is_empty());
    let removed: Vec<_> =
        checked.removed.iter().map(|r| (r.section.as_str(), r.sentence.as_str())).collect();
    assert_eq!(removed, [("Ranking", "Row [1]."), ("Findings", "It shines [2].")]);
}

#[tokio::test]
async fn a_repair_searches_under_its_own_event_and_leaves_the_ledger_alone() {
    let llm = looping(HashMap::new(), "open");
    let (tools, _) = on(&llm, Fresh::default());
    let ctx = ctx();
    let mut run = state(&tools, &ctx, 4);
    run.claims = vec![claim(1, "https://a")];
    let removed = ["One [1].", "Two [1]."].map(|sentence| RemovedSentence {
        section: "Findings".into(),
        sentence: sentence.into(),
        reason: "no".into(),
        stage: VerifyStage::Verify,
    });
    let verified = Verified {
        sections: vec![("Findings".into(), "One [1].".into())],
        removed: removed.into(),
        ..Verified::default()
    };

    let titles = ["Findings".to_owned()];
    repair(&mut run, &mut Vec::new(), &titles, 3, verified).await.unwrap();

    let fields = common::events(&ctx);
    let marker = fields.iter().position(|f| f.kind.as_deref() == Some("repair")).unwrap();
    let searching = &fields[marker + 1..];
    assert!(fields.iter().all(|f| f.stage == "verify"));
    let parents: Vec<_> = searching
        .iter()
        .filter(|f| f.kind.as_deref() == Some("attempt"))
        .map(|f| f.parent)
        .collect();
    assert_eq!(parents.len(), 4);
    assert!(parents.iter().all(|&parent| parent == Some(marker as u64)));
    let asked: HashSet<u64> = searching.iter().filter_map(|f| f.question).collect();
    assert_eq!(asked, HashSet::from([3, 4]));
    assert!(ctx.run.status(None).questions.is_empty());
}
