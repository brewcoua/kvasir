//! One run, and everything observed about it while it works.

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;

use indexmap::IndexMap;
use kvasir_types::results::{ResearchQuestion, RunResult};
use kvasir_types::runs::{
    LmCall, LmCallContent, Progress, RunDetail, RunSpan, RunStage, RunStatus, RunSummary, RunUsage,
    SearchCall, SearchContent, SearchResult, now,
};
use serde_json::{Map, Value};
use tokio::sync::mpsc;

use crate::round;
use crate::usage::Usage;

// Enough for every event and call of a long research run, and bounded so a long-lived process
// cannot grow without limit. Events are capped per class, so a flood of one (counters, pages)
// cannot evict another. A counter is a series, one event updated in place, so it is not capped.
const MAX_STEPS: usize = 1000;
const MAX_READING: usize = 2000;
const MAX_LOG: usize = 500;
const READING_KINDS: [&str; 2] = ["page", "part"];
const MAX_CALLS: usize = 2000;
const MAX_SEARCHES: usize = 1000;
/// The calls, and the searches, a `status` carries. It is sent after every event, so it holds
/// only the recent ones; a follower merges them into the detail's by id.
pub const STATUS_CALLS: usize = 50;
/// Where usage lands when it arrives before the first progress event names a stage.
const UNSTAGED: &str = "unstaged";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Queued,
    Running,
    Done,
    Failed,
    Rejected,
}

impl State {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Running => "running",
            Self::Done => "done",
            Self::Failed => "failed",
            Self::Rejected => "rejected",
        }
    }
}

/// What a follower is sent.
#[derive(Clone, Debug, PartialEq)]
pub enum Event {
    Progress(Box<Progress>),
    /// The run as it stood when the progress event before it was published, to a follower from
    /// [`Run::follow`].
    Status(Box<RunStatus>),
    /// The run reached a terminal state, which ends the stream.
    End,
}

/// One unit of work, and everything observed about it. Every method takes the lock for a short
/// section and never across an await.
#[derive(Debug)]
pub struct Run {
    pub id: String,
    pub kind: String,
    pub topic: String,
    pub created_at: f64,
    pub planned_stages: Vec<String>,
    /// Whether each call's messages and response are kept. See `runs.keep_call_content`.
    pub keep_content: bool,
    inner: Mutex<Inner>,
}

#[derive(Debug, Default)]
struct Inner {
    state: Option<State>,
    stage: Option<String>,
    started_at: Option<f64>,
    ended_at: Option<f64>,
    error: Option<String>,
    result: Option<RunResult>,
    stages: IndexMap<String, Vec<RunSpan>>,
    steps: VecDeque<Progress>,
    reading: VecDeque<Progress>,
    log: VecDeque<Progress>,
    series: Vec<Progress>,
    // Where in `series` the newest event of each (stage, series) is, finished or not.
    series_by_key: HashMap<(String, String), usize>,
    latest: Option<Progress>,
    by_model: IndexMap<String, Usage>,
    by_role: IndexMap<String, Usage>,
    by_stage: IndexMap<String, Usage>,
    embedding_tokens: u64,
    searches: u64,
    /// Each with whether it is sent a status after each progress event.
    watchers: Vec<(mpsc::UnboundedSender<Event>, bool)>,
    calls: IndexMap<u64, LmCall>,
    contents: HashMap<u64, LmCallContent>,
    next_call: u64,
    search_calls: IndexMap<u64, SearchCall>,
    search_contents: HashMap<u64, SearchContent>,
    next_search: u64,
    questions: Vec<ResearchQuestion>,
    next_event: u64,
}

fn push_capped(events: &mut VecDeque<Progress>, event: Progress, cap: usize) {
    if events.len() == cap {
        events.pop_front();
    }
    events.push_back(event);
}

impl Inner {
    fn stage_usage(&mut self) -> &mut Usage {
        let stage = self.stage.clone().unwrap_or_else(|| UNSTAGED.to_owned());
        self.by_stage.entry(stage).or_default()
    }

    fn leave_stage(&mut self, at: f64) {
        if let Some(span) = self.stage.as_ref().and_then(|s| self.stages.get_mut(s)?.last_mut())
            && span.ended_at.is_none()
        {
            span.ended_at = Some(at);
        }
    }

    fn notify(&mut self, event: &Event) {
        self.watchers.retain(|(watcher, _)| watcher.send(event.clone()).is_ok());
    }
}

impl Run {
    pub fn new(kind: &str, topic: &str, planned_stages: Vec<String>, keep_content: bool) -> Self {
        let id = uuid::Uuid::new_v4().simple().to_string()[..12].to_owned();
        Self {
            id,
            kind: kind.to_owned(),
            topic: topic.to_owned(),
            created_at: now(),
            planned_stages,
            keep_content,
            inner: Mutex::new(Inner::default()),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn state(&self) -> State {
        self.lock().state.unwrap_or(State::Queued)
    }

    pub fn start(&self) {
        let mut inner = self.lock();
        inner.state = Some(State::Running);
        inner.started_at = Some(now());
    }

    pub fn set_result(&self, result: RunResult) {
        self.lock().result = Some(result);
    }

    pub fn result(&self) -> Option<RunResult> {
        self.lock().result.clone()
    }

    /// Ends the run, as done, failed or rejected, and ends every follower's stream.
    pub fn finish(&self, state: State, error: Option<String>) {
        let mut inner = self.lock();
        let at = now();
        inner.state = Some(state);
        inner.error = error;
        inner.ended_at = Some(at);
        inner.leave_stage(at);
        inner.notify(&Event::End);
        inner.watchers.clear();
    }

    /// Records `event`, numbers it, and returns its `seq`.
    pub fn record_progress(&self, mut event: Progress) -> u64 {
        let mut inner = self.lock();
        let inner = &mut *inner;
        // When it was published: a series update takes its series' `at` below.
        let at = event.at;
        let key = event.series.clone().map(|series| (event.stage.clone(), series));
        let live =
            key.as_ref().and_then(|key| inner.series_by_key.get(key).copied()).filter(|&i| {
                let live = &inner.series[i];
                live.total.is_none_or(|total| live.done.unwrap_or(0) < total)
            });
        if let Some(i) = live {
            // An update of a counter: it overwrites the stored event and shares its `seq`, and
            // keeps its `at`, which is where the series began.
            let stored = &inner.series[i];
            event.seq = stored.seq;
            event.updated_at = Some(event.at);
            event.at = stored.at;
        } else {
            event.seq = Some(inner.next_event);
            inner.next_event += 1;
        }
        if inner.stage.as_deref() != Some(event.stage.as_str()) {
            inner.leave_stage(at);
            // Re-entering a stage adds a span to the one already there.
            let spans = inner.stages.entry(event.stage.clone()).or_default();
            spans.push(RunSpan { started_at: at, ended_at: None });
            inner.stage = Some(event.stage.clone());
        }
        if let Some(i) = live {
            let stored = &mut inner.series[i];
            stored.done = event.done;
            stored.total = event.total;
            stored.detail.clone_from(&event.detail);
            stored.updated_at = event.updated_at;
            inner.latest = Some(stored.clone());
        } else {
            let stored = event.clone();
            match (&key, stored.kind.as_deref()) {
                (Some(key), _) => {
                    inner.series_by_key.insert(key.clone(), inner.series.len());
                    inner.series.push(stored.clone());
                }
                (None, None) => push_capped(&mut inner.log, stored.clone(), MAX_LOG),
                (None, Some(kind)) if READING_KINDS.contains(&kind) => {
                    push_capped(&mut inner.reading, stored.clone(), MAX_READING);
                }
                (None, Some(_)) => push_capped(&mut inner.steps, stored.clone(), MAX_STEPS),
            }
            inner.latest = Some(stored);
        }
        let seq = event.seq.unwrap_or(0);
        inner.notify(&Event::Progress(Box::new(event)));
        // Taken here rather than when a follower gets to it, by which time the run has moved on.
        if inner.watchers.iter().any(|(_, status)| *status) {
            let status = Event::Status(Box::new(self.status_of(inner, Some(STATUS_CALLS))));
            inner
                .watchers
                .retain(|(watcher, wants)| !wants || watcher.send(status.clone()).is_ok());
        }
        seq
    }

    /// A completion was sent; returns its id. `event` is the last event of the work that made it.
    pub fn start_lm(
        &self,
        model: &str,
        role: &str,
        messages: Vec<Map<String, Value>>,
        event: Option<u64>,
    ) -> u64 {
        let mut inner = self.lock();
        let call = inner.next_call;
        inner.next_call += 1;
        if self.keep_content {
            inner
                .contents
                .insert(call, LmCallContent { messages, response: None, reasoning: None });
        }
        inner.calls.insert(
            call,
            LmCall {
                id: Some(call),
                event,
                has_content: self.keep_content,
                role: Some(role.to_owned()),
                model: model.to_owned(),
                started_at: now(),
                ended_at: None,
                prompt_tokens: None,
                completion_tokens: None,
                failed: false,
            },
        );
        // Insertion-ordered, so this forgets the oldest. A call forgotten in flight ends into
        // nothing.
        while inner.calls.len() > MAX_CALLS {
            if let Some((forgotten, _)) = inner.calls.shift_remove_index(0) {
                inner.contents.remove(&forgotten);
            }
        }
        call
    }

    /// A completion came back with its token counts, or failed with None.
    pub fn end_lm(
        &self,
        call: u64,
        tokens: Option<(u64, u64)>,
        response: Option<String>,
        reasoning: Option<String>,
    ) {
        let mut inner = self.lock();
        let Some(record) = inner.calls.get_mut(&call) else {
            return;
        };
        let ended = now();
        record.ended_at = Some(ended);
        record.prompt_tokens = tokens.map(|t| t.0);
        record.completion_tokens = tokens.map(|t| t.1);
        record.failed = tokens.is_none();
        let (model, role, seconds) =
            (record.model.clone(), record.role.clone(), ended - record.started_at);
        if let Some(content) = inner.contents.get_mut(&call) {
            content.response = response;
            content.reasoning = reasoning;
        }
        if tokens.is_some() {
            let role = role.unwrap_or_else(|| "unattributed".to_owned());
            inner.by_model.entry(model).or_default().seconds += seconds;
            inner.by_role.entry(role).or_default().seconds += seconds;
            inner.stage_usage().seconds += seconds;
        }
    }

    /// What one completion spent, attributed to the stage the run is in now. A stage can change
    /// while a call is in flight, so the split is close rather than exact.
    pub fn record_lm(&self, model: &str, role: &str, usage: kvasir_llm::Usage) {
        let mut inner = self.lock();
        let add = |u: &mut Usage| {
            u.add(usage.prompt_tokens, usage.completion_tokens, usage.reasoning_tokens, usage.cost);
        };
        add(inner.by_model.entry(model.to_owned()).or_default());
        add(inner.by_role.entry(role.to_owned()).or_default());
        add(inner.stage_usage());
    }

    pub fn record_embedding(&self, tokens: u64) {
        let mut inner = self.lock();
        inner.embedding_tokens += tokens;
        inner.stage_usage().embedding_tokens += tokens;
    }

    pub fn record_search(&self, queries: u64) {
        let mut inner = self.lock();
        inner.searches += queries;
        inner.stage_usage().searches += queries;
    }

    pub fn start_search(&self, engine: &str, query: &str, event: Option<u64>) -> u64 {
        let mut inner = self.lock();
        let search = inner.next_search;
        inner.next_search += 1;
        inner.search_calls.insert(
            search,
            SearchCall {
                id: search,
                event,
                has_content: self.keep_content,
                engine: engine.to_owned(),
                query: query.to_owned(),
                started_at: now(),
                ended_at: None,
                results: None,
                failures: Vec::new(),
            },
        );
        while inner.search_calls.len() > MAX_SEARCHES {
            if let Some((forgotten, _)) = inner.search_calls.shift_remove_index(0) {
                inner.search_contents.remove(&forgotten);
            }
        }
        search
    }

    pub fn end_search(&self, search: u64, results: Vec<SearchResult>, failures: Vec<String>) {
        let mut inner = self.lock();
        let Some(record) = inner.search_calls.get_mut(&search) else {
            return;
        };
        record.ended_at = Some(now());
        record.results = Some(results.len() as u64);
        record.failures = failures;
        if self.keep_content {
            inner.search_contents.insert(search, SearchContent { results });
        }
    }

    pub fn content(&self, call: u64) -> Option<LmCallContent> {
        self.lock().contents.get(&call).cloned()
    }

    pub fn contents(&self) -> Vec<(u64, LmCallContent)> {
        self.lock().contents.iter().map(|(id, c)| (*id, c.clone())).collect()
    }

    pub fn search_content(&self, search: u64) -> Option<SearchContent> {
        self.lock().search_contents.get(&search).cloned()
    }

    pub fn search_contents(&self) -> Vec<(u64, SearchContent)> {
        self.lock().search_contents.iter().map(|(id, c)| (*id, c.clone())).collect()
    }

    pub fn record_questions(&self, questions: Vec<ResearchQuestion>) {
        self.lock().questions = questions;
    }

    pub fn summary(&self) -> RunSummary {
        self.summary_of(&self.lock())
    }

    fn summary_of(&self, inner: &Inner) -> RunSummary {
        let sum = |pick: fn(&Usage) -> u64| inner.by_model.values().map(pick).sum::<u64>();
        let usage = RunUsage {
            prompt_tokens: sum(|u| u.prompt_tokens),
            completion_tokens: sum(|u| u.completion_tokens),
            reasoning_tokens: sum(|u| u.reasoning_tokens),
            cost: round(inner.by_model.values().map(|u| u.cost).sum(), 6),
            embedding_tokens: inner.embedding_tokens,
            searches: inner.searches,
            by_model: inner.by_model.iter().map(|(k, u)| (k.clone(), u.model())).collect(),
            by_role: inner.by_role.iter().map(|(k, u)| (k.clone(), u.model())).collect(),
            by_stage: inner.by_stage.iter().map(|(k, u)| (k.clone(), u.stage())).collect(),
        };
        RunSummary {
            id: self.id.clone(),
            kind: self.kind.clone(),
            topic: self.topic.clone(),
            state: inner.state.unwrap_or(State::Queued).as_str().to_owned(),
            stage: inner.stage.clone(),
            created_at: self.created_at,
            started_at: inner.started_at,
            ended_at: inner.ended_at,
            error: inner.error.clone(),
            usage,
            planned_stages: self.planned_stages.clone(),
            latest: inner.latest.clone(),
            has_result: inner.result.is_some(),
        }
    }

    /// The run as it stands, with its most recent `calls` and searches, or all when None.
    pub fn status(&self, calls: Option<usize>) -> RunStatus {
        self.status_of(&self.lock(), calls)
    }

    fn status_of(&self, inner: &Inner, calls: Option<usize>) -> RunStatus {
        let at = inner.ended_at.unwrap_or_else(now);
        let recent = |len: usize| calls.map_or(0, |n| len.saturating_sub(n));
        let stages = inner
            .stages
            .iter()
            .map(|(name, spans)| RunStage {
                name: name.clone(),
                started_at: spans[0].started_at,
                ended_at: spans[spans.len() - 1].ended_at,
                seconds: round(
                    spans.iter().map(|s| s.ended_at.unwrap_or(at) - s.started_at).sum(),
                    1,
                ),
                spans: spans.clone(),
            })
            .collect();
        RunStatus {
            summary: self.summary_of(inner),
            stages,
            calls: inner.calls.values().skip(recent(inner.calls.len())).cloned().collect(),
            searches: inner
                .search_calls
                .values()
                .skip(recent(inner.search_calls.len()))
                .cloned()
                .collect(),
            questions: inner.questions.clone(),
        }
    }

    pub fn detail(&self) -> RunDetail {
        let inner = self.lock();
        let mut events: Vec<Progress> = (inner.steps.iter())
            .chain(&inner.reading)
            .chain(&inner.log)
            .chain(&inner.series)
            .cloned()
            .collect();
        events.sort_by_key(|event| event.seq.unwrap_or(0));
        RunDetail { status: self.status_of(&inner, None), events, result: inner.result.clone() }
    }

    /// Follow this run live: progress events, then [`Event::End`]. A run already over sends
    /// nothing, and the receiver ends at once.
    pub fn watch(&self) -> mpsc::UnboundedReceiver<Event> {
        self.watcher(false)
    }

    /// As [`Run::watch`], with an [`Event::Status`] after each progress event.
    pub fn follow(&self) -> mpsc::UnboundedReceiver<Event> {
        self.watcher(true)
    }

    fn watcher(&self, status: bool) -> mpsc::UnboundedReceiver<Event> {
        let (sender, receiver) = mpsc::unbounded_channel();
        let mut inner = self.lock();
        if !matches!(inner.state, Some(State::Done | State::Failed | State::Rejected)) {
            inner.watchers.push((sender, status));
        }
        receiver
    }
}

#[cfg(test)]
#[path = "run_tests.rs"]
mod tests;
