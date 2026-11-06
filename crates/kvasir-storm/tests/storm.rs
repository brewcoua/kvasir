//! STORM with every model call and search faked, and the pages its editors are drawn from served
//! by a local site.

#[path = "../../kvasir-research/tests/common/mod.rs"]
mod common;

use std::collections::HashSet;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::{Results, Tiers, ctx, events, target, tools};
use kvasir_config::StormSettings;
use kvasir_fetch::{Guard, Options, Web, system_lookup};
use kvasir_llm::{BoxFuture, LlmError, Request};
use kvasir_research::{Hit, Search, Tools};
use kvasir_run::RunCtx;
use kvasir_run::stages::{ARTICLE, POLISH};
use kvasir_storm::curation::{NOTHING_FOUND, contents, converse, retrieve};
use kvasir_storm::write::Snippets;
use kvasir_testkit::{ScriptedLlm, reply};
use kvasir_types::requests::{Input, StormOptions};
use kvasir_types::sites::Tier;
use wiremock::matchers::{any, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const ROLES: [&str; 4] = ["question_asker", "conv_simulator", "outline_gen", "article_gen"];

fn hit(url: &str, snippet: &str) -> Hit {
    Hit { url: url.into(), title: url.into(), snippets: vec![snippet.into()], ..Hit::default() }
}

/// Whether the prompt of `request` holds the input field `name`.
fn asks(request: &Request<'_>, name: &str) -> bool {
    request.messages.last().unwrap().content.contains(&format!("[[ ## {name} ## ]]"))
}

/// A model that plays every STORM role: one editor beside the basic fact writer, one search query
/// a question, an outline of one section, and that section citing its first source.
fn writer() -> ScriptedLlm {
    ScriptedLlm::new(|request| {
        let said = match request.target.role {
            "question_asker" if asks(request, "examples") => {
                reply(&[("reasoning", "r"), ("personas", r#"["Historian: studies the stone"]"#)])
            }
            "question_asker" if asks(request, "persona") => {
                reply(&[("reasoning", "r"), ("question", "When was it found?")])
            }
            "question_asker" => reply(&[("reasoning", "r"), ("related_topics", "[]")]),
            "conv_simulator" if asks(request, "info") => {
                reply(&[("answer", "It was found in 1843 [1].")])
            }
            "conv_simulator" => reply(&[("queries", r#"["rok stone found"]"#)]),
            "outline_gen" => reply(&[("outline", "# History")]),
            "article_gen" if asks(request, "draft_page") => {
                reply(&[("lead_section", "The Rok stone is a runestone.")])
            }
            "article_gen" => reply(&[("written_section", "# History\nFound in 1843 [1].")]),
            role => panic!("no reply for {role}"),
        };
        Ok(said)
    })
}

fn options(polish: bool) -> StormOptions {
    StormOptions {
        search_top_k: None,
        max_conv_turn: Some(1),
        max_perspective: Some(1),
        do_polish_article: polish,
        model_fast: None,
        model_strong: None,
        lists_on: None,
        lists_off: None,
    }
}

fn web(guard: Guard) -> Web {
    let options = Options {
        contact: String::new(),
        host_interval: Duration::ZERO,
        render: false,
        stealth: false,
        max_age_hours: 48,
    };
    Web::new(options, 8, guard, system_lookup())
}

async fn write(tools: &Tools, ctx: &RunCtx, polish: bool) -> kvasir_types::results::ArticleResult {
    let input = Input { question: "Rok stone".into(), clarifications: Vec::new() };
    let defaults = StormSettings::default();
    kvasir_storm::run(tools, &web(Guard::Public), &defaults, ctx, &input, &options(polish))
        .await
        .unwrap()
}

#[tokio::test]
async fn a_run_writes_a_cited_article_with_its_lead_first() {
    let llm = Arc::new(writer());
    let mut tools = tools(&llm, &ROLES);
    tools.search = Arc::new(Results::new(vec![hit("https://a.org/rok", "found 1843")], vec![]));
    let ctx = ctx();

    let result = write(&tools, &ctx, true).await;

    assert_eq!(
        result.article,
        "# summary\n\nThe Rok stone is a runestone.\n\n# History\n\nFound in 1843 [1]."
    );
    assert_eq!(result.outline, "# History");
    let cited: Vec<(u64, &str, &str)> =
        result.citations.iter().map(|c| (c.index, c.url.as_str(), c.snippet.as_str())).collect();
    assert_eq!(cited, [(1, "https://a.org/rok", "found 1843")]);
    let events: Vec<(String, String)> =
        events(&ctx).into_iter().map(|event| (event.stage, event.detail)).collect();
    let has = |stage: &str, detail: &str| events.contains(&(stage.into(), detail.into()));
    assert!(has(ARTICLE, "writing History"));
    assert!(has(ARTICLE, "finished History (1/1)"));
    assert!(has(POLISH, "polished the article"));
}

#[tokio::test]
async fn polishing_is_skipped_when_the_request_turns_it_off() {
    let llm = Arc::new(writer());
    let mut tools = tools(&llm, &ROLES);
    tools.search = Arc::new(Results::new(vec![hit("https://a.org/rok", "found 1843")], vec![]));
    let ctx = ctx();

    let result = write(&tools, &ctx, false).await;

    assert_eq!(result.article, "# History\n\nFound in 1843 [1].");
    assert!(events(&ctx).iter().all(|event| event.stage != POLISH));
    assert!(
        llm.sent().iter().all(|sent| !sent.messages.last().unwrap().content.contains("draft_page"))
    );
}

/// A research stage that found no source still writes its sections, from nothing.
#[tokio::test]
async fn a_run_that_finds_nothing_still_writes_an_article() {
    let llm = Arc::new(writer());
    let tools = tools(&llm, &ROLES);
    let ctx = ctx();

    let result = write(&tools, &ctx, true).await;

    assert!(result.article.contains("# History"));
    // The expert says it found nothing without asking the model to answer.
    let sent = llm.sent();
    let answered = sent.iter().filter(|sent| {
        sent.role == "conv_simulator"
            && sent.messages.last().unwrap().content.contains("## info ##")
    });
    assert_eq!(answered.count(), 0);
    // The section cited a source it was never given, so the citation is dropped.
    assert!(result.citations.is_empty());
    assert!(!result.article.contains("[1]"));
}

/// Fails each query named `bad`, and finds one page for any other, recording the most searches
/// open at once.
#[derive(Default)]
struct Flaky {
    open: AtomicUsize,
    most: AtomicUsize,
    queries: Mutex<Vec<String>>,
}

impl Search for Flaky {
    fn search<'a>(
        &'a self,
        query: &'a str,
        _exclude: &'a HashSet<String>,
        _ctx: &'a RunCtx,
    ) -> BoxFuture<'a, (Vec<Hit>, Vec<String>)> {
        Box::pin(async move {
            self.queries.lock().unwrap().push(query.to_owned());
            let open = self.open.fetch_add(1, Ordering::SeqCst) + 1;
            self.most.fetch_max(open, Ordering::SeqCst);
            tokio::time::sleep(Duration::from_millis(20)).await;
            self.open.fetch_sub(1, Ordering::SeqCst);
            if query == "bad" {
                return (Vec::new(), vec!["search backend is down".into()]);
            }
            (vec![hit(&format!("https://example.org/{query}"), &format!("about {query}"))], vec![])
        })
    }
}

fn queries(names: &[&str]) -> Vec<String> {
    names.iter().map(|&name| name.to_owned()).collect()
}

#[tokio::test]
async fn one_failing_query_does_not_discard_its_siblings() {
    let llm = Arc::new(ScriptedLlm::replying(&[]));
    let mut tools = tools(&llm, &[]);
    tools.max_threads = 4;
    tools.search = Arc::new(Flaky::default());

    let found = retrieve(&tools, &ctx(), &queries(&["good", "bad", "also good"])).await;

    let titles: Vec<&str> = found.iter().map(|info| info.title.as_str()).collect();
    assert_eq!(titles, ["https://example.org/good", "https://example.org/also good"]);
}

#[tokio::test]
async fn a_query_is_searched_once_and_the_searches_stay_within_the_width() {
    let llm = Arc::new(ScriptedLlm::replying(&[]));
    let mut tools = tools(&llm, &[]);
    tools.max_threads = 2;
    let search = Arc::new(Flaky::default());
    tools.search = search.clone();
    let ctx = ctx();

    retrieve(&tools, &ctx, &queries(&["a", "b", "a", "c", "d"])).await;

    assert_eq!(*search.queries.lock().unwrap(), ["a", "b", "c", "d"]);
    assert_eq!(search.most.load(Ordering::SeqCst), 2);
    assert_eq!(ctx.run.detail().status.summary.usage.searches, 4);
}

#[tokio::test]
async fn a_result_from_a_site_to_avoid_is_dropped() {
    let llm = Arc::new(ScriptedLlm::replying(&[]));
    let mut tools = tools(&llm, &[]);
    let hits = vec![hit("https://bad.org/x", "x"), hit("https://a.org/x", "y")];
    tools.search = Arc::new(Results::new(hits, Vec::new()));
    tools.sources = Some(Arc::new(Tiers::new(&[("bad.org", Tier::Avoid)])));

    let found = retrieve(&tools, &ctx(), &queries(&["q"])).await;

    let urls: Vec<&str> = found.iter().map(|info| info.url.as_str()).collect();
    assert_eq!(urls, ["https://a.org/x"]);
}

/// Upstream recorded a canned apology as the answer, and the writer asked again.
#[tokio::test]
async fn a_failed_answer_ends_the_conversation_with_what_it_has() {
    let llm = Arc::new(ScriptedLlm::new(|request| match request.target.role {
        "question_asker" => Ok(reply(&[("reasoning", "r"), ("question", "When?")])),
        "conv_simulator" if asks(request, "info") => {
            Err(LlmError::Response { model: "m".into(), message: "broken".into() })
        }
        _ => Ok(reply(&[("queries", r#"["q"]"#)])),
    }));
    let mut tools = tools(&llm, &ROLES);
    tools.search = Arc::new(Results::new(vec![hit("https://a.org/x", "x")], Vec::new()));
    let turned = AtomicUsize::new(0);
    let count = |_: &RunCtx| {
        turned.fetch_add(1, Ordering::SeqCst);
    };

    let turns = converse(&tools, &ctx(), "Rok stone", "Historian: h", 3, &count).await.unwrap();

    assert!(turns.is_empty());
    assert_eq!(turned.load(Ordering::SeqCst), 0);
    let asked = llm.sent().iter().filter(|sent| sent.role == "question_asker").count();
    assert_eq!(asked, 1);
}

#[tokio::test]
async fn an_expert_that_found_nothing_says_so_rather_than_answering() {
    let llm = Arc::new(ScriptedLlm::new(|request| match request.target.role {
        "question_asker" => Ok(reply(&[("reasoning", "r"), ("question", "When?")])),
        _ => Ok(reply(&[("queries", r#"["q"]"#)])),
    }));
    let tools = tools(&llm, &ROLES);

    let turns = converse(&tools, &ctx(), "Rok stone", "Historian: h", 1, &|_| ()).await.unwrap();

    assert_eq!(turns.len(), 1);
    assert_eq!(turns[0].answer, NOTHING_FOUND);
}

/// Upstream's empty table had a (0, 0) embedding, which failed the similarity product and with it
/// the whole run.
#[tokio::test]
async fn a_section_query_against_nothing_collected_retrieves_nothing() {
    let llm = Arc::new(ScriptedLlm::replying(&[]));
    let tools = tools(&llm, &[]);
    let ctx = ctx();
    let snippets = Snippets::embed(&tools.encoder, &ctx, Vec::new()).await.unwrap();

    let found = snippets.nearest(&tools.encoder, &ctx, &queries(&["History"])).await.unwrap();

    assert!(found.is_empty());
}

#[test]
fn a_target_shows_no_credential() {
    let mut target = target("article_gen");
    target.model.api_key = "a-real-key".into();

    assert!(!format!("{target:?}").contains("a-real-key"));
}

const METADATA: &str = "http://169.254.169.254/latest/meta-data";

fn page() -> ResponseTemplate {
    ResponseTemplate::new(200).set_body_raw("<h1>Title</h1><h2>Section</h2>", "text/html")
}

fn moved(status: u16, to: &str) -> ResponseTemplate {
    ResponseTemplate::new(status).insert_header("location", to)
}

#[tokio::test]
async fn a_url_the_model_chose_must_be_public() {
    let web = web(Guard::Public);
    for url in
        [METADATA, "http://127.0.0.1:8080/v1/settings", "http://[::1]/", "file:///etc/passwd"]
    {
        let err = contents(&web, url).await.unwrap_err();
        assert!(err.starts_with("not a public URL"), "{url}: {err}");
    }
}

#[tokio::test]
async fn a_redirect_to_a_private_address_is_not_followed() {
    let server = MockServer::start().await;
    Mock::given(any()).respond_with(moved(302, METADATA)).mount(&server).await;

    let err = contents(&web(Guard::Loopback), &format!("{}/a", server.uri())).await.unwrap_err();

    assert!(err.starts_with("not a public URL"), "{err}");
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn a_redirect_between_public_pages_is_followed() {
    let server = MockServer::start().await;
    Mock::given(path("/wiki/a")).respond_with(moved(301, "/wiki/b")).mount(&server).await;
    Mock::given(path("/wiki/b")).respond_with(page()).mount(&server).await;

    let read = contents(&web(Guard::Loopback), &format!("{}/wiki/a", server.uri())).await;

    assert_eq!(read, Ok(("Title".into(), "Section".into())));
    let asked: Vec<String> = server
        .received_requests()
        .await
        .unwrap()
        .iter()
        .map(|request| request.url.path().to_owned())
        .collect();
    assert_eq!(asked, ["/wiki/a", "/wiki/b"]);
}

#[tokio::test]
async fn redirects_stop_at_a_limit() {
    let server = MockServer::start().await;
    Mock::given(any()).respond_with(moved(302, "/a")).mount(&server).await;

    let err = contents(&web(Guard::Loopback), &format!("{}/a", server.uri())).await.unwrap_err();

    assert!(err.starts_with("too many redirects"), "{err}");
    assert_eq!(server.received_requests().await.unwrap().len(), 6);
}
