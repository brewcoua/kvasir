//! Starting runs, listing them, and following one live, as server-sent events.
//!
//! A frame is one `event:` line, one `data:` line and a blank line. Payloads are JSON, so a value
//! never holds the raw newline that would end a frame early. A run is its own task: a client that
//! stops reading stops only its stream, and the run keeps its slot until its work ends.

use std::convert::Infallible;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::Json;
use axum::body::{Body, Bytes};
use axum::extract::{Path, State};
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use kvasir_run::{Event, Run, RunCtx, STATUS_CALLS, State as RunState};
use kvasir_sites::sources::{Sources, describe_urls};
use kvasir_types::results::RunResult;
use kvasir_types::runs::{
    ErrorEvent, LmCallContent, RunDetail, RunStarted, RunSummary, SearchContent,
};
use serde::Serialize;
use tokio::sync::{mpsc, oneshot};
use tracing::Instrument;

use crate::body::{self, Detail};
use crate::methods::{self, RunError};
use crate::{ApiError, App};

/// How often a follower gets a fresh status while nothing else happens. Tokens are spent between
/// progress events, and some stages publish none for minutes.
const STATUS_INTERVAL: Duration = Duration::from_secs(2);

pub fn routes() -> axum::Router<Arc<App>> {
    axum::Router::new()
        .route("/v1/runs", get(list).post(start))
        .route("/v1/runs/{run_id}", get(detail))
        .route("/v1/runs/{run_id}/calls/{call_id}", get(call))
        .route("/v1/runs/{run_id}/searches/{search_id}", get(search))
        .route("/v1/runs/{run_id}/events", get(events))
}

fn frame(event: &str, payload: &impl Serialize) -> Bytes {
    let data = serde_json::to_string(payload).expect("payloads serialize");
    Bytes::from(format!("event: {event}\ndata: {data}\n\n"))
}

/// A stream of the frames sent into the returned sender. A send fails once the client has gone.
fn stream() -> (mpsc::Sender<Bytes>, Response) {
    let (sender, receiver) = mpsc::channel::<Bytes>(16);
    let frames = futures::stream::unfold(receiver, |mut receiver| async move {
        receiver.recv().await.map(|frame| (Ok::<_, Infallible>(frame), receiver))
    });
    let response = (
        [
            (header::CONTENT_TYPE, "text/event-stream; charset=utf-8"),
            // Proxies that buffer a response defeat the point of streaming it.
            (header::CACHE_CONTROL, "no-cache"),
            (header::HeaderName::from_static("x-accel-buffering"), "no"),
        ],
        Body::from_stream(frames),
    )
        .into_response();
    (sender, response)
}

/// `result` with its citations' `source` as they are known now, so a mark applies to a run already
/// stored. Citations are stored without one.
pub async fn sourced(sources: Option<&Arc<Sources>>, mut result: RunResult) -> RunResult {
    let citations = match &mut result {
        RunResult::Article(article) => &mut article.citations,
        RunResult::Research(research) => &mut research.citations,
    };
    if sources.is_none() || citations.is_empty() {
        return result;
    }
    let urls: Vec<String> = citations.iter().map(|citation| citation.url.clone()).collect();
    let found = describe_urls(sources, &urls).await;
    for citation in citations.iter_mut() {
        citation.source = found.get(&citation.url).cloned();
    }
    result
}

async fn sourced_detail(sources: Option<&Arc<Sources>>, mut detail: RunDetail) -> RunDetail {
    if let Some(result) = detail.result.take() {
        detail.result = Some(sourced(sources, result).await);
    }
    detail
}

/// Runs a method and streams its progress until its result is ready.
///
/// Answers 429 rather than queueing when every slot is taken: a queued request would wait longer
/// than any sensible client timeout, and a refusal holds no slot.
async fn start(State(app): State<Arc<App>>, sent: Bytes) -> Result<Response, ApiError> {
    let value = body::json(&sent)?;
    let request = methods::request(&value)?;
    let live = app.live();
    for model in methods::models(&request).into_iter().flatten() {
        live.settings
            .resolve(model)
            .map_err(|err| ApiError::new(StatusCode::BAD_REQUEST, err.to_string()))?;
    }
    live.settings.ready()?;
    let method = methods::method(&request);
    let question = methods::input(&request).question.clone();
    // Registered before the slot is claimed, so a refusal is a run that shows up as rejected rather
    // than a 429 with nothing behind it.
    let run = app.runs.start(method.name(), &question, methods::stages(&request)).await;
    let Some(slot) = app.slots.try_claim() else {
        app.runs.finish(&run, RunState::Rejected, Some("all run slots are busy".into())).await;
        tracing::warn!("rejected {} {question:?}: every run slot is busy", method.name());
        let message = "all run slots are busy, retry later";
        return Err(ApiError::new(StatusCode::TOO_MANY_REQUESTS, message));
    };
    // Subscribed before the work starts, so no event is missed.
    let mut watcher = run.watch();
    let (done, outcome) = oneshot::channel();
    let span = tracing::info_span!("run", run_id = %run.id, run_kind = method.name());
    let work = {
        let (app, run, live) = (Arc::clone(&app), Arc::clone(&run), Arc::clone(&live));
        async move {
            let started = Instant::now();
            tracing::info!("started {} {question:?}", method.name());
            run.start();
            let ctx = RunCtx::new(Arc::clone(&run));
            let task = {
                let app = Arc::clone(&app);
                tokio::spawn(
                    async move { methods::run(&app, &live, &request, &ctx).await }
                        .in_current_span(),
                )
            };
            let result = match task.await {
                Ok(result) => result.map_err(|err: RunError| err.to_string()),
                Err(err) => Err(format!("JoinError: {err}")),
            };
            match &result {
                Ok(result) => {
                    // Before the run ends, so the closing snapshot already carries it.
                    run.set_result(result.clone());
                    app.runs.finish(&run, RunState::Done, None).await;
                }
                Err(message) => {
                    tracing::error!("{} failed: {message}", method.name());
                    app.runs.finish(&run, RunState::Failed, Some(message.clone())).await;
                }
            }
            let seconds = started.elapsed().as_secs_f64();
            tracing::info!("finished {} {question:?} in {seconds:.1}s", method.name());
            // Here rather than when the response ends, which is earlier if the client leaves.
            drop(slot);
            let _ = done.send(result);
        }
    };
    tokio::spawn(work.instrument(span));

    let (sender, response) = stream();
    let run_id = run.id.clone();
    tokio::spawn(async move {
        // First, so a client can follow the run at /v1/runs/{id} even if it stops reading here.
        if sender.send(frame("run", &RunStarted { run_id })).await.is_err() {
            return;
        }
        while let Some(Event::Progress(event)) = watcher.recv().await {
            if sender.send(frame("progress", &event)).await.is_err() {
                return;
            }
        }
        let last = match outcome.await {
            Ok(Ok(result)) => frame("done", &sourced(live.sources.as_ref(), result).await),
            Ok(Err(message)) => frame("error", &ErrorEvent { message }),
            Err(_) => frame("error", &ErrorEvent { message: "the run was lost".into() }),
        };
        let _ = sender.send(last).await;
    });
    Ok(response)
}

/// Every run kept, newest first.
async fn list(State(app): State<Arc<App>>) -> Result<Json<Vec<RunSummary>>, ApiError> {
    Ok(Json(app.runs.summaries().await?))
}

async fn detail(
    State(app): State<Arc<App>>,
    Path(run_id): Path<String>,
) -> Result<Json<RunDetail>, ApiError> {
    let detail = app.runs.detail(&run_id).await?;
    Ok(Json(sourced_detail(app.live().sources.as_ref(), detail).await))
}

/// A path's number, refused as FastAPI refused a path parameter that is not an integer.
fn number(name: &str, raw: &str) -> Result<u64, ApiError> {
    raw.parse().map_err(|_| {
        ApiError::Invalid(vec![Detail {
            kind: "int_parsing",
            loc: body::loc(&["path", name]),
            msg: "Input should be a valid integer, unable to parse string as an integer".into(),
            input: raw.into(),
            ctx: None,
        }])
    })
}

/// What one model call was sent and answered. Kept only with runs.keep_call_content.
async fn call(
    State(app): State<Arc<App>>,
    Path((run_id, call_id)): Path<(String, String)>,
) -> Result<Json<LmCallContent>, ApiError> {
    let call_id = number("call_id", &call_id)?;
    Ok(Json(app.runs.content(&run_id, call_id).await?))
}

/// What one search returned. Kept only with runs.keep_call_content.
async fn search(
    State(app): State<Arc<App>>,
    Path((run_id, search_id)): Path<(String, String)>,
) -> Result<Json<SearchContent>, ApiError> {
    let search_id = number("search_id", &search_id)?;
    let mut content = app.runs.search_content(&run_id, search_id).await?;
    let urls: Vec<String> = content.results.iter().map(|result| result.url.clone()).collect();
    let found = describe_urls(app.live().sources.as_ref(), &urls).await;
    for result in &mut content.results {
        result.source = found.get(&result.url).cloned();
    }
    Ok(Json(content))
}

/// Follow a run live, including one this client did not start.
///
/// Opens with a snapshot, so a client attaching midway sees what it missed. Each progress event is
/// followed by a status, as is every idle interval, so usage and stage timings stay current
/// between snapshots. A run only the store holds has ended, so its stream is the one snapshot.
async fn events(
    State(app): State<Arc<App>>,
    Path(run_id): Path<String>,
) -> Result<Response, ApiError> {
    let sources = app.live().sources.clone();
    let Some(run) = app.runs.get(&run_id) else {
        let detail = sourced_detail(sources.as_ref(), app.runs.detail(&run_id).await?).await;
        let (sender, response) = stream();
        sender.send(frame("snapshot", &detail)).await.expect("the receiver is held");
        return Ok(response);
    };
    let (sender, response) = stream();
    tokio::spawn(follow(run, sources, sender));
    Ok(response)
}

async fn follow(run: Arc<Run>, sources: Option<Arc<Sources>>, sender: mpsc::Sender<Bytes>) {
    // Subscribed before the snapshot is taken, so a run that ends in between still ends the
    // stream rather than leaving the client waiting on an end that already happened.
    let mut watcher = run.follow();
    let detail = sourced_detail(sources.as_ref(), run.detail()).await;
    let ended = detail.status.summary.ended_at.is_some();
    if sender.send(frame("snapshot", &detail)).await.is_err() || ended {
        return;
    }
    loop {
        let sent = match tokio::time::timeout(STATUS_INTERVAL, watcher.recv()).await {
            Ok(Some(Event::Progress(event))) => frame("progress", &event),
            Ok(Some(Event::Status(status))) => frame("status", &status),
            Ok(Some(Event::End) | None) => break,
            Err(_idle) => frame("status", &run.status(Some(STATUS_CALLS))),
        };
        if sender.send(sent).await.is_err() {
            return;
        }
    }
    let _ =
        sender.send(frame("snapshot", &sourced_detail(sources.as_ref(), run.detail()).await)).await;
}
