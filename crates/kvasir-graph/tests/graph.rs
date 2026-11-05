//! The graph method, with every model call, search and page faked.

#[path = "../../kvasir-research/tests/common/mod.rs"]
mod common;

use std::collections::hash_map::DefaultHasher;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::hash::{Hash, Hasher};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::{Fetched, Results, at, ctx, tools};
use kvasir_config::ResearchSettings;
use kvasir_graph::explore::explore;
use kvasir_graph::graph::{branches, path};
use kvasir_graph::proof::{NEVER, pick, work};
use kvasir_graph::{open_questions, outline, research};
use kvasir_ledger::frontier::Frontier;
use kvasir_ledger::questions::Question;
use kvasir_ledger::search_loop::Research;
use kvasir_llm::{BoxFuture, Request};
use kvasir_research::corpus::Corpus;
use kvasir_research::{Hit, Search, Tools};
use kvasir_run::RunCtx;
use kvasir_testkit::{ScriptedLlm, reply};
use kvasir_types::requests::{GraphOptions, Input};
use kvasir_types::results::QuestionStatus;
use kvasir_types::runs::Progress;
use serde_json::json;
use tokio::time::Instant;

use QuestionStatus::{Answered, Open, Unresolved, Weak};

/// Each text its own pseudo-random vector, so no two texts are close.
fn scattered(text: &str) -> Vec<f32> {
    let mut hasher = DefaultHasher::new();
    text.hash(&mut hasher);
    let mut state = hasher.finish();
    (0..64)
        .map(|_| {
            // splitmix64
            state = state.wrapping_add(0x9e37_79b9_7f4a_7c15);
            let mut z = state;
            z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
            z ^= z >> 31;
            (z as f64 / u64::MAX as f64 * 2.0 - 1.0) as f32
        })
        .collect()
}

fn question(id: u64, text: &str) -> Question {
    Question::new(id, text)
}

fn under(id: u64, text: &str, parent: u64, depth: u64) -> Question {
    Question { parent: Some(parent), depth, ..Question::new(id, text) }
}

fn tried(question: Question, status: QuestionStatus, searches: usize) -> Question {
    Question { status, queries: vec!["q".into(); searches], ..question }
}

#[test]
fn a_path_runs_from_the_first_question_and_a_branch_holds_its_descendants() {
    let questions = [question(1, "a"), under(2, "b", 1, 1), under(3, "c", 2, 2), question(4, "d")];
    let ids = |found: Vec<&Question>| found.iter().map(|q| q.id).collect::<Vec<_>>();

    assert_eq!(ids(path(&questions, &questions[2])), [1, 2]);
    assert!(path(&questions, &questions[0]).is_empty());
    let grouped: Vec<(u64, Vec<u64>)> =
        branches(&questions).into_iter().map(|(root, branch)| (root, ids(branch))).collect();
    assert_eq!(grouped, [(1, vec![1, 2, 3]), (4, vec![4])]);
}

/// The value of the field `name` in the prompt of `request`.
fn field(request: &Request<'_>, name: &str) -> String {
    let prompt = &request.messages.last().unwrap().content;
    let marker = format!("[[ ## {name} ## ]]\n");
    let value = prompt.split(&marker).nth(1).unwrap_or_else(|| panic!("no {name} in {prompt}"));
    value.split("\n\n").next().unwrap().to_owned()
}

type Status = Box<dyn Fn(&str, &str) -> &'static str + Send + Sync>;
type Raised = Box<dyn Fn(&str) -> Vec<String> + Send + Sync>;
type Asked = Box<dyn Fn() -> Vec<String> + Send + Sync>;

/// How the fakes of an exploration answer. A search for a question finds `found(question)` new
/// claims, and its judgement gives it `status(question, path)` and raises `raised(question)`.
struct Setup {
    budget: Option<usize>,
    max_depth: u64,
    max_attempts: usize,
    deadline: Duration,
    late: bool,
    status: Status,
    raised: Raised,
    found: Box<dyn Fn(&str) -> usize + Send + Sync>,
    gaps: Asked,
}

impl Default for Setup {
    fn default() -> Self {
        Self {
            budget: Some(10),
            max_depth: 2,
            max_attempts: 3,
            deadline: Duration::from_secs(1_000_000),
            late: false,
            status: Box::new(|_, _| "answered"),
            raised: Box::new(|question| vec![format!("{question} more")]),
            found: Box::new(|_| 1),
            gaps: Box::new(Vec::new),
        }
    }
}

/// Answers each query with one page never seen before, after `wait`.
#[derive(Default)]
struct Fresh {
    wait: Duration,
    queries: Mutex<usize>,
}

impl Search for Fresh {
    fn search<'a>(
        &'a self,
        _query: &'a str,
        _exclude: &'a HashSet<String>,
        _ctx: &'a RunCtx,
    ) -> BoxFuture<'a, (Vec<Hit>, Vec<String>)> {
        let mut queries = self.queries.lock().unwrap();
        *queries += 1;
        let url = format!("https://example.org/{queries}");
        let hit = Hit { url: url.clone(), title: url, ..Hit::default() };
        Box::pin(async move {
            tokio::time::sleep(self.wait).await;
            (vec![hit], Vec::new())
        })
    }
}

struct Explored {
    searches: usize,
    /// The (path, question) of each judgement.
    judged: Vec<(String, String)>,
    ctx: RunCtx,
}

/// The model the fakes of `setup` describe, recording each judgement in `judged`.
fn model(setup: Setup, judged: Arc<Mutex<Vec<(String, String)>>>) -> ScriptedLlm {
    let claims = Mutex::new(0);
    ScriptedLlm::new(move |request| {
        Ok(match request.target.role {
            "plan" => reply(&[("query", &field(request, "question"))]),
            "intake" => {
                let mut made = claims.lock().unwrap();
                let found: Vec<_> = (0..(setup.found)(&field(request, "question")))
                    .map(|_| {
                        *made += 1;
                        json!({"claim": format!("claim {made}"), "quote": "piece", "confidence": "high"})
                    })
                    .collect();
                reply(&[("findings", &json!(found).to_string())])
            }
            "decompose" => {
                let raised: Vec<_> =
                    (setup.gaps)().iter().map(|q| json!({"title": "gap", "question": q})).collect();
                reply(&[("raised", &json!(raised).to_string())])
            }
            "check" => {
                let (path, asked) = (field(request, "path"), field(request, "question"));
                judged.lock().unwrap().push((path.clone(), asked.clone()));
                let ids: Vec<u64> = field(request, "evidence")
                    .lines()
                    .filter_map(|line| line.strip_prefix('[')?.split(']').next()?.parse().ok())
                    .collect();
                let raised: Vec<_> = (setup.raised)(&asked)
                    .iter()
                    .map(|q| json!({"title": "more", "question": q}))
                    .collect();
                reply(&[
                    ("status", (setup.status)(&asked, &path)),
                    ("claims", &json!(ids).to_string()),
                    ("missing", ""),
                    ("raised", &json!(raised).to_string()),
                ])
            }
            role => panic!("no reply for {role}"),
        })
    })
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

async fn explored(questions: &mut Vec<Question>, setup: Setup) -> Explored {
    explored_with(questions, setup, Duration::ZERO).await
}

/// Explores `questions` as `setup` says, each search taking `wait`.
async fn explored_with(questions: &mut Vec<Question>, setup: Setup, wait: Duration) -> Explored {
    let judged = Arc::new(Mutex::new(Vec::new()));
    let (budget, max_depth, max_attempts) = (setup.budget, setup.max_depth, setup.max_attempts);
    let started = Instant::now();
    let deadline = started + setup.deadline;
    let late_at = if setup.late { started } else { deadline };
    let llm = Arc::new(model(setup, judged.clone()).with_embedding(scattered));
    let mut tools = tools(&llm, &["plan", "intake", "check", "decompose"]);
    tools.search = Arc::new(Fresh { wait, ..Fresh::default() });
    tools.pages = Arc::new(Fetched { any: Some(at("https://any", "piece")), ..Fetched::default() });
    let ctx = ctx();
    let mut run = state(&tools, &ctx, max_attempts);
    let mut frontier = Frontier::new(&tools.encoder, "brief", &ctx).await.unwrap();
    let searches =
        explore(&mut run, &mut frontier, questions, deadline, late_at, budget, max_depth)
            .await
            .unwrap();
    let judged = judged.lock().unwrap().clone();
    Explored { searches, judged, ctx }
}

fn shape(questions: &[Question]) -> Vec<(&str, Option<u64>, u64, QuestionStatus)> {
    questions.iter().map(|q| (q.text.as_str(), q.parent, q.depth, q.status)).collect()
}

fn texts(questions: &[Question]) -> Vec<&str> {
    questions.iter().map(|q| q.text.as_str()).collect()
}

fn details(ctx: &RunCtx) -> Vec<String> {
    common::events(ctx).into_iter().map(|e| e.detail).collect()
}

fn pairs(judged: &[(&str, &str)]) -> Vec<(String, String)> {
    judged.iter().map(|(p, q)| ((*p).to_owned(), (*q).to_owned())).collect()
}

#[tokio::test]
async fn a_judgement_reads_its_path_and_raises_children_down_to_the_depth_cap() {
    let mut questions = vec![question(1, "a")];
    let setup = Setup {
        budget: Some(2),
        max_depth: 1,
        status: Box::new(|_, _| "weak"),
        ..Setup::default()
    };

    let explored = explored(&mut questions, setup).await;

    // The child is judged under its parent, and at the depth cap raises nothing.
    assert_eq!(explored.searches, 2);
    assert_eq!(explored.judged, pairs(&[("none", "a"), ("a", "a more")]));
    assert_eq!(shape(&questions), [("a", None, 0, Weak), ("a more", Some(1), 1, Weak)]);
}

#[tokio::test]
async fn an_answered_question_raises_one_harder_follow_up() {
    let mut questions = vec![question(1, "a")];
    let setup = Setup {
        raised: Box::new(|q| ["x", "y", "z"].map(|i| format!("{q} {i}")).into()),
        ..Setup::default()
    };

    let explored = explored(&mut questions, setup).await;

    // Each answered, down to the depth cap.
    assert_eq!(explored.searches, 3);
    let raised: Vec<_> = questions.iter().map(|q| (q.text.as_str(), q.parent)).collect();
    assert_eq!(raised, [("a", None), ("a x", Some(1)), ("a x x", Some(2))]);
}

#[tokio::test]
async fn a_question_earns_children_as_it_is_searched_again() {
    let mut questions = vec![question(1, "a")];
    let setup = Setup {
        budget: Some(1),
        status: Box::new(|_, _| "weak"),
        raised: Box::new(|q| (0..5).map(|i| format!("{q} {i}")).collect()),
        ..Setup::default()
    };

    explored(&mut questions, setup).await;

    // Searched once, it holds ceil(2 * 1 ** 0.5) children.
    assert_eq!(texts(&questions), ["a", "a 0", "a 1"]);
}

#[tokio::test(start_paused = true)]
async fn the_research_stops_at_its_deadline() {
    let mut questions = vec![question(1, "a"), question(2, "b")];
    let setup = Setup { deadline: Duration::from_secs(5), ..Setup::default() };

    let explored = explored_with(&mut questions, setup, Duration::from_secs(10)).await;

    assert_eq!(explored.searches, 1);
}

#[tokio::test]
async fn a_parent_is_judged_again_once_its_children_are_answered() {
    let mut questions = vec![question(1, "a")];
    let judgements = Mutex::new(0);
    let setup = Setup {
        max_depth: 1,
        status: Box::new(move |q, _| {
            if q != "a" {
                return "answered";
            }
            let mut seen = judgements.lock().unwrap();
            *seen += 1;
            if *seen == 1 { "weak" } else { "answered" }
        }),
        ..Setup::default()
    };

    let explored = explored(&mut questions, setup).await;

    // Its child's claims answer `a` without another search for it.
    assert_eq!(explored.searches, 2);
    assert_eq!(explored.judged.last().unwrap(), &("none".to_owned(), "a".to_owned()));
    assert_eq!((questions[0].status, questions[0].attempts()), (Answered, 1));
}

#[tokio::test]
async fn an_attempt_is_followed_by_its_result() {
    let setup = Setup { budget: Some(1), max_depth: 0, found: Box::new(|_| 2), ..Setup::default() };

    let explored = explored(&mut vec![question(1, "a")], setup).await;

    let fields = common::events(&explored.ctx);
    let of = |kind: &str| -> Vec<&Progress> {
        fields.iter().filter(|f| f.kind.as_deref() == Some(kind)).collect()
    };
    let [attempt] = of("attempt")[..] else { panic!("one attempt") };
    let [result] = of("result")[..] else { panic!("one result") };
    assert_eq!(attempt.question, Some(1));
    assert_eq!((attempt.done, attempt.total), (Some(1), Some(1)));
    assert_eq!(result.parent, attempt.seq);
    assert_eq!((result.done, result.total), (Some(2), Some(2)));
}

#[tokio::test]
async fn a_branch_closes_once_two_searches_in_a_row_add_nothing() {
    let mut questions = vec![question(1, "a"), question(2, "b")];
    let setup = Setup {
        status: Box::new(|_, _| "weak"),
        raised: Box::new(|_| Vec::new()),
        found: Box::new(|q| if q == "a" { 4 } else { 0 }),
        ..Setup::default()
    };

    let explored = explored(&mut questions, setup).await;

    assert_eq!((questions[0].attempts(), questions[1].attempts()), (3, 2));
    let closed = "closing the branch of b: 2 searches in a row added nothing new";
    assert!(details(&explored.ctx).iter().any(|d| d == closed));
}

#[test]
fn work_is_the_cheaper_of_a_retry_and_settling_every_child() {
    let questions = [
        tried(question(1, "a"), Weak, 1),
        tried(question(2, "b"), Weak, 2),
        tried(under(3, "c", 2, 1), Answered, 1),
        under(4, "d", 2, 1),
        tried(question(5, "e"), Unresolved, 3),
        question(6, "f"),
    ];

    let found = work(&questions, 3, &HashSet::from([6]));

    let expected = HashMap::from([(1, 2.0), (2, 1.0), (3, 0.0), (4, 1.0), (5, NEVER), (6, NEVER)]);
    assert_eq!(found, expected);
}

async fn frontier(llm: &Arc<ScriptedLlm>) -> (Frontier, Tools, RunCtx) {
    let tools = tools(llm, &[]);
    let ctx = ctx();
    let frontier = Frontier::new(&tools.encoder, "brief", &ctx).await.unwrap();
    (frontier, tools, ctx)
}

async fn picked(
    (frontier, tools, ctx): &mut (Frontier, Tools, RunCtx),
    questions: &[Question],
    late: bool,
) -> Option<u64> {
    let found = pick(frontier, &tools.encoder, ctx, questions, &[], 3, late).await.unwrap();
    found.map(|i| questions[i].id)
}

fn scattering() -> Arc<ScriptedLlm> {
    Arc::new(ScriptedLlm::replying(&[]).with_embedding(scattered))
}

fn alike() -> Arc<ScriptedLlm> {
    Arc::new(ScriptedLlm::replying(&[]).with_vectors(&[], Some(&[1.0, 0.0])))
}

#[tokio::test]
async fn the_pick_walks_down_to_a_child_while_the_children_cost_no_more_than_a_retry() {
    let mut questions =
        vec![tried(question(1, "a"), Weak, 1), under(2, "c2", 1, 1), under(3, "c3", 1, 1)];
    let mut at = frontier(&scattering()).await;

    // A retry costs 2, the two untried children 2 together: the children go first.
    assert!(matches!(picked(&mut at, &questions, false).await, Some(2 | 3)));
    // With a third, they cost more than the retry.
    questions.push(under(4, "c4", 1, 1));
    assert_eq!(picked(&mut at, &questions, false).await, Some(1));
    at.0.closed.insert(1);
    assert_eq!(picked(&mut at, &questions, false).await, None);
}

/// A first question answered down to an untried grandchild, and one weak after one search.
fn unequal_first_questions() -> Vec<Question> {
    vec![
        tried(question(1, "a"), Answered, 1),
        tried(under(2, "b", 1, 1), Answered, 1),
        under(3, "c", 2, 2),
        tried(question(4, "x"), Weak, 1),
    ]
}

#[tokio::test]
async fn a_first_question_weak_after_one_search_goes_before_two_levels_down() {
    let mut questions = unequal_first_questions();
    let mut at = frontier(&alike()).await;

    assert_eq!(picked(&mut at, &questions, false).await, Some(4));
    // Searched twice, it waits its turn.
    questions[3].queries.push("q".into());
    assert_eq!(picked(&mut at, &questions, false).await, Some(3));
}

#[tokio::test]
async fn late_in_the_research_a_first_question_left_goes_first() {
    let mut questions = unequal_first_questions();
    questions[3].queries.push("q".into());
    let mut at = frontier(&alike()).await;

    assert_eq!(picked(&mut at, &questions, true).await, Some(4));
}

#[tokio::test]
async fn late_in_the_research_no_question_raises_others_and_no_gap_is_asked() {
    let mut questions = vec![question(1, "a")];
    let setup = Setup {
        late: true,
        status: Box::new(|_, _| "weak"),
        gaps: Box::new(|| vec!["b".into()]),
        ..Setup::default()
    };

    let explored = explored(&mut questions, setup).await;

    assert_eq!(texts(&questions), ["a"]);
    assert_eq!(explored.searches, 3);
    let last = details(&explored.ctx).pop().unwrap();
    assert_eq!(last, "stopping: no question left to settle, and too little time");
}

#[tokio::test]
async fn a_run_writes_one_section_per_branch_that_found_something() {
    let named = |questions: &[(&str, &str)]| {
        let named: Vec<_> =
            questions.iter().map(|(t, q)| json!({"title": t, "question": q})).collect();
        json!(named).to_string()
    };
    let brief = reply(&[
        ("brief", "b"),
        ("assumptions", "[]"),
        ("output", "report"),
        ("criteria", "[]"),
        ("entities", "[]"),
    ]);
    let split = reply(&[("questions", &named(&[("first", "Q1?"), ("second", "Q2?")]))]);
    let plans = [reply(&[("query", "one")]), reply(&[("query", "two")])];
    let finding = r#"[{"claim": "Found in 1904", "quote": "found in 1904", "confidence": "high"}]"#;
    let intake = reply(&[("findings", finding)]);
    let check = reply(&[
        ("status", "answered"),
        ("claims", "[1]"),
        ("missing", ""),
        ("raised", &named(&[("third", "Q3?")])),
    ]);
    let outlined = reply(&[("sections", r#"[{"title": "The find", "questions": [1]}]"#)]);
    let write = reply(&[("text", "It was found in 1904 [1].")]);
    let tldr = reply(&[("points", r#"["It was found in **1904** [1]."]"#)]);
    let supported = reply(&[("verdict", "supported"), ("reason", "the quote gives the year")]);
    let replies: &[(&str, &[&str])] = &[
        ("brief", &[&brief]),
        ("decompose", &[&split]),
        ("plan", &[&plans[0], &plans[1]]),
        ("intake", &[&intake]),
        ("check", &[&check]),
        ("outline", &[&outlined]),
        ("write", &[&write]),
        ("tldr", &[&tldr]),
        ("verify", &[&supported, &supported]),
    ];
    let vectors: &[(&str, &[f32])] = &[
        ("b", &[1.0, 0.0, 0.0]),
        ("Q1?", &[0.0, 1.0, 0.0]),
        ("Q2?", &[0.0, 0.0, 1.0]),
        ("Q3?", &[0.0, 1.0, 1.0]),
        ("Found in 1904", &[0.0, 1.0, 0.0]),
    ];
    let llm = Arc::new(ScriptedLlm::by_role(replies).with_vectors(vectors, Some(&[1.0, 1.0, 1.0])));
    let roles =
        &["brief", "decompose", "plan", "intake", "check", "outline", "write", "tldr", "verify"];
    let mut tools = tools(&llm, roles);
    let hit = Hit { url: "https://a".into(), title: "A".into(), ..Hit::default() };
    tools.search = Arc::new(Results::new(vec![hit], Vec::new()));
    let page = at("https://a", "The stone\n\nIt was found in 1904 near Kvasir.");
    tools.pages = Arc::new(Fetched::of([page]));
    let input = Input { question: "When was the stone found?".into(), clarifications: Vec::new() };
    let options =
        GraphOptions { max_searches: Some(2), max_depth: Some(1), ..GraphOptions::default() };

    let result =
        research(&tools, &ResearchSettings::default(), &ctx(), &input, &options, "2026-11-01")
            .await
            .unwrap();

    // Q2's search finds only the page already read; Q3 is raised under Q1 but never reached.
    assert_eq!(
        result.report,
        "> [!SUMMARY] TL;DR\n> - It was found in **1904** [1].\n\n\
         ## The find\n\nIt was found in 1904 [1].\n\n## Open questions\n\n- Q2? (not found)"
    );
    let asked: Vec<_> = result
        .questions
        .iter()
        .map(|q| (q.title.as_str(), q.text.as_str(), q.parent, q.status))
        .collect();
    assert_eq!(
        asked,
        [
            ("first", "Q1?", None, Answered),
            ("second", "Q2?", None, Open),
            ("third", "Q3?", Some(1), Open),
        ]
    );
    let cited: Vec<_> = result.citations.iter().map(|c| c.url.as_str()).collect();
    assert_eq!(cited, ["https://a"]);
}

#[tokio::test]
async fn the_follow_ups_of_an_answered_question_are_still_reached() {
    let questions = [tried(question(1, "a"), Answered, 1), under(2, "b", 1, 1)];
    let mut at = frontier(&scattering()).await;

    assert_eq!(work(&questions, 3, &HashSet::new()), HashMap::from([(1, 1.0), (2, 1.0)]));
    assert_eq!(picked(&mut at, &questions, false).await, Some(2));
}

#[tokio::test]
async fn children_all_answered_leave_an_unanswered_question_to_a_retry() {
    let mut questions = [
        tried(question(1, "a"), Weak, 1),
        tried(under(2, "b", 1, 1), Answered, 1),
        under(3, "c", 2, 2),
    ];
    let mut at = frontier(&scattering()).await;

    assert_eq!(work(&questions, 3, &HashSet::new())[&1], 2.0);
    assert_eq!(picked(&mut at, &questions, false).await, Some(1));
    // Given up, what is left below it is its children's follow-ups.
    questions[0].status = Unresolved;
    assert_eq!(picked(&mut at, &questions, false).await, Some(3));
}

type Outlined = Vec<(String, Vec<String>, BTreeSet<u64>)>;

/// The outline of `questions` from a model replying `sections`, and the questions it was shown.
async fn outlined(
    questions: &[Question],
    sections: serde_json::Value,
    vectors: &[(&str, &[f32])],
) -> (Outlined, String) {
    let replied = reply(&[("sections", &sections.to_string())]);
    let llm = Arc::new(
        ScriptedLlm::by_role(&[("outline", &[&replied])]).with_vectors(vectors, Some(&[1.0, 0.0])),
    );
    let tools = tools(&llm, &["outline"]);
    let found = outline(&tools, &ctx(), "brief", questions).await.unwrap();
    let prompt = llm.sent()[0].messages.last().unwrap().content.clone();
    (found, prompt)
}

fn holding(question: Question, claims: &[u64]) -> Question {
    Question { claims: claims.to_vec(), ..question }
}

fn titled(question: Question, title: &str) -> Question {
    Question { title: title.into(), ..question }
}

fn section(title: &str, subsections: &[&str], ids: &[u64]) -> (String, Vec<String>, BTreeSet<u64>) {
    let subsections = subsections.iter().map(|&s| s.to_owned()).collect();
    (title.to_owned(), subsections, ids.iter().copied().collect())
}

#[tokio::test]
async fn the_outline_merges_a_repeated_title_and_files_a_left_out_question_closest() {
    let questions = [
        holding(titled(question(1, "Q1?"), "first"), &[1]),
        holding(titled(question(2, "Q2?"), "second"), &[2]),
        holding(under(3, "Q3?", 1, 1), &[3]),
        holding(titled(question(4, "Q4?"), "fourth"), &[4]),
        question(5, "Q5?"),
    ];
    let sections = json!([
        {"title": "Causes", "questions": [1], "subsections": ["Drought"]},
        {"title": "Effects", "questions": [3, 2]},
        {"title": "causes", "questions": [2], "subsections": ["Drought", "Trade"]},
        {"title": "Nothing", "questions": [5]},
    ]);
    let vectors: &[(&str, &[f32])] =
        &[("Causes", &[1.0, 0.0]), ("Effects", &[0.0, 1.0]), ("Q4?", &[0.1, 1.0])];

    let (found, shown) = outlined(&questions, sections, vectors).await;

    assert_eq!(
        found,
        [section("Causes", &["Drought", "Trade"], &[1, 2]), section("Effects", &[], &[2, 3, 4])]
    );
    // Only questions with claims are shown.
    assert!(shown.contains("[4] fourth: Q4? (open, 1 claims)"), "{shown}");
    assert!(!shown.contains("[5]"), "{shown}");
}

#[tokio::test]
async fn the_outline_keeps_seven_sections_at_most() {
    let questions: Vec<Question> =
        (1..10).map(|n| holding(question(n, &format!("Q{n}?")), &[n])).collect();
    let sections: Vec<_> =
        (1..10).map(|n| json!({"title": format!("S{n}"), "questions": [n]})).collect();

    let (found, _) = outlined(&questions, json!(sections), &[]).await;

    let titles: Vec<&str> = found.iter().map(|(title, _, _)| title.as_str()).collect();
    assert_eq!(titles, ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]);
    let ids: BTreeSet<u64> = found.into_iter().flat_map(|(_, _, ids)| ids).collect();
    assert_eq!(ids, (1..10).collect());
}

#[tokio::test]
async fn an_outline_with_no_usable_section_gives_each_branch_one() {
    let questions = [
        holding(titled(question(1, "Q1?"), "first"), &[1]),
        holding(under(2, "Q2?", 1, 1), &[2]),
        holding(question(3, "Q3?"), &[3]),
    ];

    let (found, _) = outlined(&questions, json!([{"title": "Empty", "questions": [9]}]), &[]).await;

    assert_eq!(found, [section("first", &[], &[1, 2]), section("Q3?", &[], &[3])]);
}

/// Gap calls replying each of `asked` in turn.
fn gap_calls(asked: Vec<Vec<&'static str>>) -> Asked {
    let asked = Mutex::new(asked.into_iter());
    Box::new(move || {
        let next = asked.lock().unwrap().next().expect("a gap call left");
        next.into_iter().map(str::to_owned).collect()
    })
}

#[tokio::test]
async fn once_no_question_is_left_a_gap_call_adds_first_questions_until_two_add_none() {
    let mut questions = vec![question(1, "a")];
    let setup = Setup {
        max_depth: 1,
        raised: Box::new(|_| Vec::new()),
        found: Box::new(|_| 2),
        gaps: gap_calls(vec![vec!["b", "c"], vec![], vec!["d"], vec![], vec![]]),
        ..Setup::default()
    };

    let explored = explored(&mut questions, setup).await;

    let raised: Vec<_> = questions.iter().map(|q| (q.text.as_str(), q.parent)).collect();
    assert_eq!(raised, [("a", None), ("b", None), ("c", None), ("d", None)]);
    assert_eq!(explored.searches, 4);
    let gaps = details(&explored.ctx).iter().filter(|d| d.starts_with("no question left")).count();
    assert_eq!(gaps, 5);
}

#[tokio::test]
async fn the_gap_calls_stop_once_the_questions_two_added_found_little() {
    let mut questions = vec![question(1, "a")];
    let setup = Setup {
        max_depth: 1,
        raised: Box::new(|_| Vec::new()),
        gaps: gap_calls(vec![vec!["b", "c"], vec!["d"], vec!["e"]]),
        ..Setup::default()
    };

    let explored = explored(&mut questions, setup).await;

    // One claim a search: each gap call's questions count as dry, and two in a row end it.
    assert_eq!(texts(&questions), ["a", "b", "c", "d"]);
    assert_eq!(explored.searches, 4);
    let last = details(&explored.ctx).pop().unwrap();
    assert_eq!(last, "stopping: the last 2 gaps found little or nothing new");
}

#[test]
fn open_questions_are_the_first_questions_left_then_raised_ones_answered_in_part() {
    let questions = [
        tried(question(1, "Q1?"), Open, 2),
        tried(under(2, "Q2?", 1, 1), Weak, 1),
        tried(under(3, "Q3?", 1, 1), Unresolved, 1),
        tried(question(4, "Q4?"), Answered, 1),
        question(5, "Q5?"),
    ];

    assert_eq!(
        open_questions(&questions),
        "- Q1? (not found)\n- Q5? (not searched)\n- Q2? (partly answered)"
    );
}
