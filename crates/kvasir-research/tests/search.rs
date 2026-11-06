//! Choosing which results to read: SearXNG's score fused with closeness to the question.

mod common;

use std::collections::HashSet;
use std::sync::Arc;

use common::{Results, Tiers, ctx, tools};
use kvasir_research::search::{MAX_QUERY_CHARACTERS, Ranking, search};
use kvasir_research::{Hit, Tools};
use kvasir_testkit::ScriptedLlm;
use kvasir_types::sites::Tier;

const QUESTION: &str = "when was the stone found";

fn hit(name: &str, score: f64, domain: Option<&str>) -> Hit {
    Hit {
        url: format!("https://{}.org/{name}", domain.unwrap_or(name)),
        title: name.into(),
        description: String::new(),
        snippets: vec![String::new()],
        score,
        depth: None,
    }
}

fn names(hits: &[Hit]) -> Vec<&str> {
    hits.iter().map(|hit| hit.url.rsplit('/').next().unwrap()).collect()
}

fn with(hits: Vec<Hit>, vectors: &[(&str, &[f32])], default: Option<&[f32]>) -> Tools {
    let llm = Arc::new(ScriptedLlm::replying(&[]).with_vectors(vectors, default));
    let mut tools = tools(&llm, &[]);
    tools.search = Arc::new(Results::new(hits, Vec::new()));
    tools
}

fn plain(question: &str) -> Ranking<'_> {
    Ranking { encoder: None, question, held: None, corpus: None, claims: &[] }
}

fn encoded<'a>(tools: &'a Tools, held: Option<&'a [Vec<f32>]>) -> Ranking<'a> {
    Ranking { encoder: Some(&tools.encoder), held, ..plain(QUESTION) }
}

async fn pick(
    tools: &Tools,
    per_query: usize,
    seen: &mut HashSet<String>,
    ranking: Ranking<'_>,
) -> Vec<Hit> {
    search(tools, &ctx(), &["q".into()], per_query, seen, ranking).await.unwrap()
}

#[tokio::test]
async fn a_low_scored_hit_that_matches_the_question_beats_a_high_scored_one_that_does_not() {
    let hits = vec![hit("far", 3.0, None), hit("near", 2.0, None), hit("other", 1.0, None)];
    let vectors: &[(&str, &[f32])] = &[
        (QUESTION, &[1.0, 0.0]),
        ("far\n", &[0.0, 1.0]),
        ("near\n", &[1.0, 0.0]),
        ("other\n", &[1.0, 1.0]),
    ];
    let tools = with(hits, vectors, None);

    let picked = pick(&tools, 1, &mut HashSet::new(), encoded(&tools, None)).await;

    assert_eq!(names(&picked), ["near"]);
}

#[tokio::test]
async fn hits_are_taken_by_score_not_by_response_order() {
    let hits = vec![hit("low", 1.0, None), hit("high", 5.0, None), hit("mid", 3.0, None)];
    let tools = with(hits, &[], None);

    let picked = pick(&tools, 2, &mut HashSet::new(), plain(QUESTION)).await;

    assert_eq!(names(&picked), ["high", "mid"]);
}

#[tokio::test]
async fn a_near_duplicate_snippet_and_a_repeated_domain_are_skipped() {
    let hits = vec![
        hit("a", 4.0, None),
        hit("copy", 3.0, None),
        hit("same-site", 2.0, Some("a")),
        hit("other", 1.0, None),
    ];
    let vectors: &[(&str, &[f32])] = &[
        (QUESTION, &[1.0, 0.0]),
        ("a\n", &[1.0, 0.0]),
        ("copy\n", &[1.0, 0.1]),
        ("same-site\n", &[0.0, 1.0]),
        ("other\n", &[0.5, 1.0]),
    ];
    let tools = with(hits, vectors, None);
    let mut seen = HashSet::new();

    let picked = pick(&tools, 3, &mut seen, encoded(&tools, None)).await;

    assert_eq!(names(&picked), ["a", "other"]);
    assert_eq!(seen, picked.iter().map(|hit| hit.url.clone()).collect());
}

#[tokio::test]
async fn urls_already_seen_are_still_excluded() {
    let tools = with(vec![hit("a", 2.0, None), hit("b", 1.0, None)], &[], Some(&[1.0, 0.0]));
    let mut seen = HashSet::from(["https://a.org/a".to_owned()]);

    let picked = pick(&tools, 2, &mut seen, encoded(&tools, None)).await;

    assert_eq!(names(&picked), ["b"]);
}

#[tokio::test]
async fn without_an_encoder_the_score_order_and_the_domain_skip_apply() {
    let hits = vec![hit("x", 1.0, None), hit("y", 3.0, Some("x")), hit("z", 2.0, None)];
    let tools = with(hits, &[], None);

    let picked = pick(&tools, 3, &mut HashSet::new(), plain(QUESTION)).await;

    assert_eq!(names(&picked), ["y", "z"]);
}

#[tokio::test]
async fn what_the_run_holds_lowers_a_hit_it_repeats() {
    let vectors: &[(&str, &[f32])] =
        &[(QUESTION, &[1.0, 1.0]), ("old\n", &[1.0, 0.0]), ("new\n", &[0.1, 1.0])];
    let tools = with(vec![hit("old", 2.0, None), hit("new", 1.0, None)], vectors, None);
    let held = vec![vec![1.0, 0.0]];

    let picked = pick(&tools, 1, &mut HashSet::new(), encoded(&tools, Some(&held))).await;

    assert_eq!(names(&picked), ["new"]);
}

async fn sent(query: &str) -> String {
    let results = Arc::new(Results::new(Vec::new(), Vec::new()));
    let mut tools = with(Vec::new(), &[], None);
    tools.search = results.clone();
    search(&tools, &ctx(), &[query.into()], 3, &mut HashSet::new(), plain(QUESTION)).await.unwrap();
    let queries = results.queries.lock().unwrap().clone();
    assert_eq!(queries.len(), 1);
    queries[0].clone()
}

#[tokio::test]
async fn a_degenerate_query_reaches_searxng_capped_with_balanced_quotes() {
    let terms: Vec<String> = (12..400).map(|n| format!("\"Linux 6.{n}\"")).collect();
    let query = format!("{} \"Linux 6.4", terms.join(" "));

    let sent = sent(&query).await;

    assert!(query.len() > 10 * MAX_QUERY_CHARACTERS);
    assert!(sent.chars().count() <= MAX_QUERY_CHARACTERS);
    assert_eq!(sent.matches('"').count() % 2, 0);
    assert!(sent.ends_with('"'));
    assert!(query.starts_with(&sent));
}

#[tokio::test]
async fn a_cut_leaves_no_dangling_operator() {
    let query = format!("{}OR -{}", "word ".repeat(59), "x".repeat(50));

    assert_eq!(sent(&query).await, "word ".repeat(59).trim_end());
}

#[tokio::test]
async fn a_short_query_is_sent_as_written() {
    assert_eq!(sent("\"a b\" OR c").await, "\"a b\" OR c");
}

#[tokio::test]
async fn a_result_of_an_avoid_site_is_dropped_even_when_it_is_the_only_one() {
    let mut tools = with(vec![hit("only", 1.0, None)], &[], None);
    tools.sources = Some(Arc::new(Tiers::new(&[("only.org", Tier::Avoid)])));
    let mut seen = HashSet::new();

    let picked = pick(&tools, 3, &mut seen, plain(QUESTION)).await;

    assert!(picked.is_empty());
    // Marked seen, so the next query does not classify it again.
    assert_eq!(seen, HashSet::from(["https://only.org/only".to_owned()]));
}

#[tokio::test]
async fn the_pool_of_a_query_is_classified_once() {
    let hits = vec![hit("bad", 3.0, None), hit("a", 2.0, None), hit("b", 1.0, None)];
    let mut tools = with(hits, &[], None);
    let sources = Arc::new(Tiers::new(&[("bad.org", Tier::Avoid)]));
    tools.sources = Some(sources.clone());

    let picked = pick(&tools, 3, &mut HashSet::new(), plain(QUESTION)).await;

    assert_eq!(names(&picked), ["a", "b"]);
    let hosts: HashSet<String> = ["bad.org", "a.org", "b.org"].map(str::to_owned).into();
    assert_eq!(*sources.calls.lock().unwrap(), [hosts]);

    // Two queries are still one batch.
    let queries = ["q".to_owned(), "r".to_owned()];
    search(&tools, &ctx(), &queries, 3, &mut HashSet::new(), plain(QUESTION)).await.unwrap();
    assert_eq!(sources.calls.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn the_tier_orders_results_the_other_rankings_leave_tied() {
    let hits = vec![hit("a", 3.0, None), hit("b", 2.0, None), hit("c", 1.0, None)];
    let vectors: &[(&str, &[f32])] = &[
        (QUESTION, &[1.0, 0.0]),
        ("a\n", &[0.0, 1.0]),
        ("b\n", &[1.0, 1.0]),
        ("c\n", &[1.0, 0.0]),
    ];
    let mut tools = with(hits, vectors, None);

    let untiered = pick(&tools, 3, &mut HashSet::new(), encoded(&tools, None)).await;
    tools.sources = Some(Arc::new(Tiers::new(&[("a.org", Tier::Low), ("c.org", Tier::High)])));
    let picked = pick(&tools, 3, &mut HashSet::new(), encoded(&tools, None)).await;

    assert_eq!(names(&picked), ["c", "b", "a"]);
    assert_eq!(names(&untiered), ["a", "c", "b"]);
}

#[tokio::test]
async fn searches_that_all_fail_end_the_run() {
    let mut tools = with(Vec::new(), &[], None);
    tools.search = Arc::new(Results::new(Vec::new(), vec!["google: timeout".into(); 2]));
    let queries = ["q".to_owned(), "r".to_owned()];

    let failed = search(&tools, &ctx(), &queries, 3, &mut HashSet::new(), plain(QUESTION))
        .await
        .unwrap_err();

    assert_eq!(failed.to_string(), "2 searches in a row failed: google: timeout");
}

mod providers {
    use std::collections::VecDeque;
    use std::sync::Mutex;
    use std::time::Duration;

    use kvasir_config::{SearchSettings, SearxngSettings};
    use kvasir_fetch::{Guard, Options, Web, system_lookup};
    use kvasir_llm::BoxFuture;
    use kvasir_research::Search;
    use kvasir_research::providers::{Failure, Provider, Providers, Row};
    use serde_json::json;
    use wiremock::matchers::{path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::*;

    fn web() -> Arc<Web> {
        let options = Options {
            contact: String::new(),
            host_interval: Duration::ZERO,
            render: false,
            stealth: false,
            max_age_hours: 48,
        };
        Arc::new(Web::new(options, 8, Guard::Public, system_lookup()))
    }

    fn row(url: &str, snippet: &str) -> Row {
        Row { url: url.into(), title: url.into(), snippet: snippet.into() }
    }

    /// Answers each query with the next of `answers`, then with nothing.
    struct Stub {
        name: &'static str,
        interval: Duration,
        answers: Mutex<VecDeque<Result<Vec<Row>, Failure>>>,
        calls: Arc<Mutex<usize>>,
    }

    impl Stub {
        fn boxed(name: &'static str, answers: Vec<Result<Vec<Row>, Failure>>) -> Box<dyn Provider> {
            Box::new(Self {
                name,
                interval: Duration::ZERO,
                answers: Mutex::new(answers.into()),
                calls: Arc::default(),
            })
        }
    }

    impl Provider for Stub {
        fn name(&self) -> &'static str {
            self.name
        }

        fn interval(&self) -> Duration {
            self.interval
        }

        fn query<'a>(&'a self, _: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
            *self.calls.lock().unwrap() += 1;
            let answer = self.answers.lock().unwrap().pop_front().unwrap_or(Ok(Vec::new()));
            Box::pin(async move { answer })
        }
    }

    async fn ask(providers: &Providers) -> (Vec<Hit>, Vec<String>) {
        providers.search("a", &HashSet::new(), &ctx()).await
    }

    fn urls(hits: &[Hit]) -> Vec<&str> {
        hits.iter().map(|hit| hit.url.as_str()).collect()
    }

    #[tokio::test]
    async fn a_url_two_providers_found_ranks_first_and_keeps_both_snippets() {
        let providers = Providers::new(
            vec![
                (Stub::boxed("one", vec![Ok(vec![row("a", "from one"), row("b", "")])]), 1.0),
                (Stub::boxed("two", vec![Ok(vec![row("c", ""), row("b", "from two")])]), 1.0),
            ],
            web(),
        );

        let (hits, failures) = ask(&providers).await;

        assert_eq!(urls(&hits), ["b", "a", "c"]);
        assert_eq!(hits[0].snippets, ["from two"]);
        assert_eq!(hits[0].description, "from two");
        assert!(failures.is_empty());
    }

    #[tokio::test]
    async fn a_provider_weighs_as_its_weight_says() {
        let lists = |one, two| {
            Providers::new(
                vec![
                    (Stub::boxed("one", vec![Ok(vec![row("a", "")])]), one),
                    (Stub::boxed("two", vec![Ok(vec![row("b", "")])]), two),
                ],
                web(),
            )
        };

        assert_eq!(urls(&ask(&lists(2.0, 1.0)).await.0), ["a", "b"]);
        assert_eq!(urls(&ask(&lists(1.0, 2.0)).await.0), ["b", "a"]);
    }

    #[tokio::test]
    async fn a_failed_provider_is_named_and_the_others_carry_the_query() {
        let providers = Providers::new(
            vec![
                (Stub::boxed("down", vec![Err(Failure::Other("broke".into()))]), 1.0),
                (Stub::boxed("up", vec![Ok(vec![row("a", "")])]), 1.0),
            ],
            web(),
        );

        let (hits, failures) = ask(&providers).await;

        assert_eq!(urls(&hits), ["a"]);
        assert_eq!(failures, ["down: broke"]);
    }

    #[tokio::test]
    async fn no_provider_is_a_failure_rather_than_an_empty_answer() {
        let (hits, failures) = ask(&Providers::new(Vec::new(), web())).await;

        assert!(hits.is_empty());
        assert_eq!(failures, ["no search provider is enabled"]);
    }

    #[tokio::test(start_paused = true)]
    async fn a_rate_limited_provider_rests_for_as_long_as_it_was_told() {
        let limited = Failure::RateLimited { retry_after: Some(Duration::from_secs(5)) };
        let providers = Providers::new(
            vec![(Stub::boxed("p", vec![Err(limited), Ok(vec![row("a", "")])]), 1.0)],
            web(),
        );

        assert_eq!(ask(&providers).await.1, ["p: rate limited"]);
        assert_eq!(ask(&providers).await.1, ["p: cooling down"]);
        tokio::time::advance(Duration::from_secs(6)).await;
        assert_eq!(urls(&ask(&providers).await.0), ["a"]);
    }

    #[tokio::test(start_paused = true)]
    async fn a_provider_out_of_quota_rests_until_the_next_day() {
        let providers = Providers::new(
            vec![(Stub::boxed("p", vec![Err(Failure::QuotaExhausted)]), 1.0)],
            web(),
        );

        assert_eq!(ask(&providers).await.1, ["p: out of quota"]);
        assert_eq!(ask(&providers).await.1, ["p: cooling down"]);
        tokio::time::advance(Duration::from_secs(24 * 60 * 60)).await;
        assert!(ask(&providers).await.1.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn a_refused_key_stays_refused() {
        let providers =
            Providers::new(vec![(Stub::boxed("p", vec![Err(Failure::Unauthorized)]), 1.0)], web());

        assert_eq!(ask(&providers).await.1, ["p: refused its API key"]);
        tokio::time::advance(Duration::from_secs(7 * 24 * 60 * 60)).await;
        assert_eq!(ask(&providers).await.1, ["p: refused its API key"]);
    }

    #[tokio::test(start_paused = true)]
    async fn a_query_goes_without_a_provider_its_limiter_holds() {
        let calls = Arc::new(Mutex::new(0));
        let slow = Stub {
            name: "slow",
            interval: Duration::from_secs(3),
            answers: Mutex::default(),
            calls: Arc::clone(&calls),
        };
        let providers = Providers::new(vec![(Box::new(slow), 1.0)], web());

        assert!(ask(&providers).await.1.is_empty());
        assert_eq!(ask(&providers).await.1, ["slow: waiting on its rate limit"]);
        assert_eq!(*calls.lock().unwrap(), 1);
        tokio::time::advance(Duration::from_secs(3)).await;
        assert!(ask(&providers).await.1.is_empty());
        assert_eq!(*calls.lock().unwrap(), 2);
    }

    async fn searxng(url: &str) -> (Vec<Hit>, Vec<String>) {
        let settings = SearchSettings {
            searxng: SearxngSettings { enabled: true, url: url.into(), weight: 1.0 },
            ..SearchSettings::default()
        };
        ask(&Providers::from_settings(&settings, web())).await
    }

    #[tokio::test]
    async fn no_provider_is_asked_when_searxng_is_off() {
        let settings = SearchSettings {
            searxng: SearxngSettings { enabled: false, ..SearxngSettings::default() },
            ..SearchSettings::default()
        };

        let (_, failures) = ask(&Providers::from_settings(&settings, web())).await;

        assert_eq!(failures, ["no search provider is enabled"]);
    }

    /// SearXNG answers its base URL with a 308 to /search; not following it read an HTML page as
    /// an empty answer, and the run collected nothing.
    #[tokio::test]
    async fn the_redirect_to_search_is_followed_and_results_rank_by_searxng_score() {
        let server = MockServer::start().await;
        let to = format!("{}/search?q=a&format=json", server.uri());
        let moved = ResponseTemplate::new(308).insert_header("location", to.as_str());
        Mock::given(path("/")).respond_with(moved).mount(&server).await;
        let results = json!({"results": [
            {"url": "https://a.org/y"},
            {"url": "https://a.org/x", "content": "about x", "score": 2.5},
        ]});
        Mock::given(path("/search"))
            .and(query_param("format", "json"))
            .respond_with(ResponseTemplate::new(200).set_body_json(results))
            .mount(&server)
            .await;

        let (hits, failures) = searxng(&server.uri()).await;

        assert_eq!(urls(&hits), ["https://a.org/x", "https://a.org/y"]);
        assert_eq!(hits[0].snippets, ["about x"]);
        assert!(failures.is_empty());
    }

    /// A rate-limited engine answers with no results, which reads as a topic nothing covers.
    #[tokio::test]
    async fn an_empty_answer_says_which_engines_failed() {
        let server = MockServer::start().await;
        let answer = json!({
            "results": [],
            "unresponsive_engines": [["brave", "Suspended: too many requests"]],
        });
        Mock::given(path("/"))
            .respond_with(ResponseTemplate::new(200).set_body_json(answer))
            .mount(&server)
            .await;

        let (hits, failures) = searxng(&server.uri()).await;

        assert!(hits.is_empty());
        assert_eq!(failures, ["SearXNG: brave: Suspended: too many requests"]);
    }

    #[tokio::test]
    async fn a_429_from_searxng_is_a_rate_limit() {
        let server = MockServer::start().await;
        Mock::given(path("/"))
            .respond_with(ResponseTemplate::new(429).insert_header("retry-after", "120"))
            .mount(&server)
            .await;

        let (hits, failures) = searxng(&server.uri()).await;

        assert!(hits.is_empty());
        assert_eq!(failures, ["SearXNG: rate limited"]);
    }

    #[tokio::test]
    async fn an_unreachable_instance_is_a_failure_rather_than_an_empty_answer() {
        let free = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", free.local_addr().unwrap());
        drop(free);

        let (hits, failures) = searxng(&url).await;

        assert!(hits.is_empty());
        assert_eq!(failures.len(), 1);
        assert!(failures[0].starts_with("SearXNG: request: "), "{}", failures[0]);
    }
}
