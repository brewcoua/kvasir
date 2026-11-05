//! What a run records: stages, events, calls, usage, and its followers.

use std::sync::Arc;

use kvasir_types::results::{ArticleResult, ResearchResult, RunResult};
use kvasir_types::runs::Progress;
use serde_json::{Map, Value};
use tempfile::TempDir;

use super::*;
use crate::{RunCtx, RunRegistry, RunStore};

fn run() -> Run {
    Run::new("research", "t", Vec::new(), false)
}

fn step(stage: &str, detail: &str, kind: Option<&str>) -> Progress {
    Progress { kind: kind.map(str::to_owned), ..Progress::new(stage, detail) }
}

fn counter(done: u64, total: u64) -> Progress {
    Progress {
        done: Some(done),
        total: Some(total),
        series: Some("sentences".to_owned()),
        ..Progress::new("verify", format!("checked {done}"))
    }
}

fn usage(prompt: u64, completion: u64, reasoning: u64, cost: f64) -> kvasir_llm::Usage {
    kvasir_llm::Usage {
        prompt_tokens: prompt,
        completion_tokens: completion,
        reasoning_tokens: reasoning,
        cost,
    }
}

#[tokio::test]
async fn a_watcher_sees_each_event_then_the_end() {
    let run = Arc::new(run());
    let mut watcher = run.watch();
    let worker = Arc::clone(&run);
    tokio::spawn(async move {
        worker.start();
        worker.record_progress(Progress::new("research", "asking questions"));
        worker.record_progress(Progress::new("article", "writing"));
        worker.finish(State::Done, None);
    });

    let mut received = Vec::new();
    while let Some(Event::Progress(event)) = watcher.recv().await {
        received.push(event.detail);
    }

    assert_eq!(received, ["asking questions", "writing"]);
}

#[tokio::test]
async fn watching_a_finished_run_ends_at_once() {
    let run = run();
    run.finish(State::Done, None);
    assert_eq!(run.watch().recv().await, None);
}

#[test]
fn a_reentered_stage_sums_its_spans() {
    let run = run();
    for (stage, at) in [("search", 0.0), ("write", 10.0), ("search", 12.0), ("write", 20.0)] {
        run.record_progress(Progress { at, ..Progress::new(stage, stage) });
    }

    let stages = run.status(None).stages;
    let search = stages.iter().find(|s| s.name == "search").unwrap();
    let write = stages.iter().find(|s| s.name == "write").unwrap();
    assert_eq!(search.spans.len(), 2);
    assert_eq!(search.seconds, 18.0);
    assert_eq!(write.spans[0].ended_at, Some(12.0));
}

#[test]
fn calls_show_in_flight_then_finished_or_failed() {
    let run = run();
    let answered = run.start_lm("openai/fast", "intake", Vec::new(), None);
    let failed = run.start_lm("openai/fast", "intake", Vec::new(), None);
    run.start_lm("openai/strong", "write", Vec::new(), None);

    run.end_lm(answered, Some((100, 20)), None, None);
    run.end_lm(failed, None, None, None);

    let calls = run.status(None).calls;
    let seen: Vec<_> =
        calls.iter().map(|c| (c.role.as_deref().unwrap(), c.prompt_tokens, c.failed)).collect();
    assert_eq!(
        seen,
        [("intake", Some(100), false), ("intake", None, true), ("write", None, false)]
    );
    assert!(calls[0].ended_at.is_some());
    assert!(calls[2].ended_at.is_none());
}

#[test]
fn only_the_most_recent_calls_are_kept() {
    let run = Run::new("research", "t", Vec::new(), true);
    let first = run.start_lm("m", "intake", Vec::new(), None);
    for _ in 0..MAX_CALLS {
        run.start_lm("m", "intake", Vec::new(), None);
    }

    // Forgotten while in flight, so ending it changes nothing.
    run.end_lm(first, Some((1, 1)), None, None);

    let calls = run.detail().status.calls;
    assert_eq!(calls.len(), MAX_CALLS);
    assert!(calls.iter().all(|call| call.ended_at.is_none()));
    assert_eq!(run.content(first), None);
}

#[test]
fn a_status_carries_only_the_recent_calls() {
    let run = run();
    for _ in 0..STATUS_CALLS + 5 {
        run.start_lm("m", "intake", Vec::new(), None);
    }

    assert_eq!(run.status(Some(STATUS_CALLS)).calls[0].id, Some(5));
    assert_eq!(run.detail().status.calls.len(), STATUS_CALLS + 5);
}

fn target(role: &'static str) -> kvasir_llm::Target {
    kvasir_llm::Target {
        model: kvasir_config::Model {
            name: "m".to_owned(),
            api_base: String::new(),
            api_key: String::new(),
        },
        role,
        max_tokens: 1,
        temperature: 0.0,
        top_p: None,
        reasoning_effort: None,
    }
}

#[test]
fn a_call_is_linked_to_the_event_before_it_in_its_own_work() {
    use kvasir_llm::Sink;
    let ctx = RunCtx::new(Arc::new(run()));
    ctx.started(&target("brief"), &[]);
    ctx.publish(Progress::new("research", "searching"));
    ctx.publish(Progress::new("research", "reading"));
    let task = ctx.clone();
    // Published after the work was handed out, so that work does not see it.
    ctx.publish(Progress::new("research", "read"));
    task.started(&target("intake"), &[]);
    ctx.started(&target("notes"), &[]);

    let detail = ctx.run.detail();
    let seqs: Vec<_> = detail.events.iter().map(|e| e.seq.unwrap()).collect();
    assert_eq!(seqs, [0, 1, 2]);
    let calls: Vec<_> =
        detail.status.calls.iter().map(|c| (c.role.clone().unwrap(), c.event)).collect();
    assert_eq!(
        calls,
        [("brief".into(), None), ("intake".into(), Some(1)), ("notes".into(), Some(2))]
    );
    assert_eq!(detail.status.calls[0].model, "openai/m");
}

#[test]
fn a_series_is_one_event_and_flooding_it_keeps_the_steps() {
    let run = run();
    for i in 0..3 {
        run.record_progress(step("research", &format!("s{i}"), Some("attempt")));
    }
    let first = counter(0, 6000);
    let first_at = first.at;
    run.record_progress(first);
    for done in 1..=5000 {
        run.record_progress(counter(done, 6000));
    }

    let events = run.detail().events;
    let series: Vec<_> = events.iter().filter(|e| e.series.is_some()).collect();
    assert_eq!(events.iter().filter(|e| e.kind.as_deref() == Some("attempt")).count(), 3);
    assert_eq!(series.len(), 1);
    assert_eq!((series[0].seq, series[0].done, series[0].at), (Some(3), Some(5000), first_at));
    assert!(series[0].updated_at.is_some());
    assert_eq!(events.last().unwrap().seq, Some(3));
}

#[test]
fn a_finished_series_starts_a_new_event_on_its_next_update() {
    let run = run();
    run.record_progress(counter(0, 2));
    run.record_progress(counter(2, 2));
    let again = run.record_progress(counter(0, 3));
    run.record_progress(counter(1, 3));

    let series: Vec<_> = run
        .detail()
        .events
        .into_iter()
        .filter(|e| e.series.is_some())
        .map(|e| (e.seq.unwrap(), e.done.unwrap(), e.total.unwrap()))
        .collect();
    assert_eq!(series, [(0, 2, 2), (1, 1, 3)]);
    assert_eq!(again, 1);
}

#[test]
fn a_call_after_a_series_update_is_linked_to_its_shared_seq() {
    use kvasir_llm::Sink;
    let ctx = RunCtx::new(Arc::new(run()));
    ctx.publish(counter(0, 5));
    ctx.publish(Progress::new("verify", "other"));
    let update = ctx.publish(counter(1, 5));
    ctx.started(&target("verify"), &[]);

    assert_eq!(update, 0);
    assert_eq!(ctx.run.detail().status.calls[0].event, Some(0));
}

#[test]
fn each_class_of_event_is_evicted_on_its_own() {
    let run = run();
    run.record_progress(step("research", "a", Some("attempt")));
    for i in 0..MAX_READING + 5 {
        run.record_progress(step("research", &format!("p{i}"), Some("page")));
    }
    for i in 0..MAX_LOG + 5 {
        run.record_progress(step("research", &format!("l{i}"), None));
    }

    let events = run.detail().events;
    let count = |kind: Option<&str>| events.iter().filter(|e| e.kind.as_deref() == kind).count();
    assert_eq!(
        (count(Some("attempt")), count(Some("page")), count(None)),
        (1, MAX_READING, MAX_LOG)
    );
    assert!(events.windows(2).all(|pair| pair[0].seq < pair[1].seq));
}

#[test]
fn latest_is_the_updated_counter_even_when_later_events_exist() {
    let run = run();
    run.record_progress(counter(0, 5));
    run.record_progress(step("verify", "later", Some("attempt")));
    run.record_progress(counter(3, 5));

    let latest = run.summary().latest.unwrap();
    assert_eq!((latest.seq, latest.done), (Some(0), Some(3)));
}

fn message() -> Vec<Map<String, Value>> {
    let Value::Object(message) = serde_json::json!({"role": "user", "content": "q"}) else {
        unreachable!()
    };
    vec![message]
}

#[test]
fn call_content_is_kept_only_when_asked() {
    for keep in [false, true] {
        let run = Run::new("research", "t", Vec::new(), keep);
        let call = run.start_lm("m", "intake", message(), None);
        run.end_lm(call, Some((10, 2)), Some("an answer".into()), Some("thinking".into()));

        assert_eq!(run.detail().status.calls[0].has_content, keep);
        let content = run.content(call);
        if keep {
            let content = content.unwrap();
            assert_eq!(content.messages, message());
            assert_eq!(content.response.as_deref(), Some("an answer"));
            assert_eq!(content.reasoning.as_deref(), Some("thinking"));
        } else {
            assert_eq!(content, None);
        }
    }
}

#[test]
fn only_answered_calls_add_their_time_to_usage() {
    let run = run();
    let failed = run.start_lm("m", "intake", Vec::new(), None);
    run.end_lm(failed, None, None, None);
    assert!(!run.summary().usage.by_model.contains_key("m"));

    let answered = run.start_lm("m", "intake", Vec::new(), None);
    std::thread::sleep(std::time::Duration::from_millis(20));
    run.end_lm(answered, Some((10, 40)), None, None);
    assert!(run.summary().usage.by_model["m"].seconds >= 0.02);
}

#[test]
fn usage_is_split_by_the_stage_it_was_spent_in() {
    let run = run();
    run.record_lm("m", "conv_simulator", usage(30, 5, 0, 0.0));
    run.record_progress(Progress::new("research", "asking questions"));
    run.record_lm("m", "conv_simulator", usage(10, 5, 0, 0.001));
    run.record_search(2);
    run.record_progress(Progress::new("article", "writing"));
    run.record_lm("m", "article_gen", usage(100, 40, 25, 0.002));
    run.record_embedding(300);

    let usage = run.summary().usage;
    let by_stage = &usage.by_stage;
    assert_eq!(by_stage.keys().collect::<Vec<_>>(), ["unstaged", "research", "article"]);
    assert_eq!(by_stage["unstaged"].model.prompt_tokens, 30);
    assert_eq!(by_stage["research"].model.prompt_tokens, 10);
    assert_eq!(by_stage["research"].searches, 2);
    assert_eq!(by_stage["article"].model.completion_tokens, 40);
    assert_eq!(by_stage["article"].model.reasoning_tokens, 25);
    assert_eq!(by_stage["article"].embedding_tokens, 300);
    assert_eq!((usage.reasoning_tokens, usage.cost), (25, 0.003));
    assert_eq!(usage.by_role["conv_simulator"].calls, 2);
}

#[test]
fn a_failed_search_says_why_and_results_are_kept_only_when_asked() {
    for keep in [false, true] {
        let run = Run::new("research", "t", Vec::new(), keep);
        let search = run.start_search("searxng", "q", None);
        run.end_search(search, Vec::new(), vec!["timeout".into()]);

        let call = &run.detail().status.searches[0];
        assert_eq!((call.results, call.failures.clone()), (Some(0), vec!["timeout".to_owned()]));
        assert_eq!(run.search_content(search).is_some(), keep);
    }
}

#[tokio::test]
async fn the_registry_forgets_the_oldest() {
    let registry = RunRegistry::new(2, None, false);
    let first = registry.start("storm", "one", Vec::new()).await;
    registry.start("storm", "two", Vec::new()).await;
    registry.start("storm", "three", Vec::new()).await;

    assert!(registry.get(&first.id).is_none());
    let topics: Vec<_> = registry.summaries().await.unwrap().into_iter().map(|s| s.topic).collect();
    assert_eq!(topics, ["three", "two"]);
}

fn store(dir: &TempDir, ttl_days: u64, limit: usize) -> RunStore {
    RunStore::open(&dir.path().join("kvasir.db"), ttl_days, limit).unwrap()
}

#[tokio::test]
async fn a_run_survives_a_restart_with_its_type_and_content() {
    let dir = TempDir::new().unwrap();
    let registry = RunRegistry::new(10, Some(store(&dir, 30, 10)), true);
    let results = [
        RunResult::Research(ResearchResult {
            report: "r".into(),
            assumptions: vec!["a".into()],
            duration_seconds: 1.0,
            ..ResearchResult::default()
        }),
        RunResult::Article(ArticleResult {
            article: "a".into(),
            outline: "o".into(),
            citations: Vec::new(),
            duration_seconds: 1.0,
        }),
    ];
    let mut ids = Vec::new();
    for result in &results {
        let run = registry.start("research", "t", Vec::new()).await;
        let call = run.start_lm("m", "intake", message(), None);
        run.end_lm(call, Some((1, 1)), Some("answer".into()), None);
        let search = run.start_search("searxng", "q", None);
        run.end_search(search, Vec::new(), Vec::new());
        run.set_result(result.clone());
        registry.finish(&run, State::Done, None).await;
        ids.push(run.id.clone());
    }

    let later = RunRegistry::new(10, Some(store(&dir, 30, 10)), true);
    for (id, result) in ids.iter().zip(&results) {
        let detail = later.detail(id).await.unwrap();
        assert_eq!(detail.result.as_ref(), Some(result));
        assert_eq!(detail.status.summary.state, "done");
        assert_eq!(later.content(id, 0).await.unwrap().response.as_deref(), Some("answer"));
        assert!(later.search_content(id, 0).await.is_ok());
    }
    assert!(matches!(later.content(&ids[0], 9).await, Err(crate::NotFound::Call(_))));
    assert!(matches!(later.detail("nope").await, Err(crate::NotFound::Run(_))));
    assert_eq!(later.summaries().await.unwrap().len(), 2);
}

#[tokio::test]
async fn a_run_cut_short_by_a_restart_is_marked_interrupted() {
    let dir = TempDir::new().unwrap();
    let run = RunRegistry::new(10, Some(store(&dir, 30, 10)), false)
        .start("storm", "t", Vec::new())
        .await;

    let later = store(&dir, 30, 10);
    later.sweep().await.unwrap();

    let summaries = later.summaries().await.unwrap();
    assert_eq!(summaries.len(), 1);
    assert_eq!(summaries[0].state, "failed");
    assert_eq!(summaries[0].error.as_deref(), Some(crate::INTERRUPTED));
    assert_eq!(summaries[0].ended_at, Some(summaries[0].created_at));
    assert_eq!(later.detail(&run.id).await.unwrap().unwrap().status.summary.state, "failed");
}

#[tokio::test]
async fn the_store_keeps_only_what_retention_allows() {
    let dir = TempDir::new().unwrap();
    let registry = RunRegistry::new(10, Some(store(&dir, 1, 2)), false);
    let old = registry.start("storm", "old", Vec::new()).await;
    kvasir_db::rusqlite::Connection::open(dir.path().join("kvasir.db"))
        .unwrap()
        .execute("UPDATE runs SET created_at = created_at - 2 * 86400", [])
        .unwrap();
    for topic in ["one", "two", "three"] {
        let run = registry.start("storm", topic, Vec::new()).await;
        registry.finish(&run, State::Done, None).await;
    }

    let kept = store(&dir, 1, 2);
    let topics: Vec<_> = kept.summaries().await.unwrap().into_iter().map(|s| s.topic).collect();
    assert_eq!(topics, ["three", "two"]);
    assert!(kept.detail(&old.id).await.unwrap().is_none());
}

#[tokio::test]
async fn citing_finds_runs_whose_result_holds_a_url() {
    let dir = TempDir::new().unwrap();
    let registry = RunRegistry::new(10, Some(store(&dir, 30, 10)), false);
    let run = registry.start("research", "t", Vec::new()).await;
    run.set_result(RunResult::Research(ResearchResult {
        citations: vec![kvasir_types::results::Citation {
            index: 1,
            url: "https://a.example/x".into(),
            title: String::new(),
            snippet: String::new(),
            source: None,
        }],
        ..ResearchResult::default()
    }));
    registry.finish(&run, State::Done, None).await;
    let url = ["https://a.example/x".to_owned()];

    assert_eq!(registry.citing(&url).await.unwrap().len(), 1);
    let later = RunRegistry::new(10, Some(store(&dir, 30, 10)), false);
    assert_eq!(later.citing(&url).await.unwrap().len(), 1);
    assert!(later.citing(&["https://b.example".to_owned()]).await.unwrap().is_empty());
}
