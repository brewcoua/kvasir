//! One fast-model call each, answered without registering a run or claiming a slot: the user is
//! still deciding whether to start one, or the client is doing its own housekeeping.

use std::sync::Arc;

use axum::Json;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::post;
use kvasir_llm::roles;
use kvasir_llm::{Message, NoSink, Request, TASK_RETRY_FOR};
use kvasir_types::requests::{ClarifyRequest, ClarifyResponse, TaskRequest, TaskResponse};
use serde::de::DeserializeOwned;

use crate::body::{self, Bounds};
use crate::{ApiError, App};

pub fn routes() -> axum::Router<Arc<App>> {
    axum::Router::new().route("/v1/clarify", post(clarify)).route("/v1/tasks", post(task))
}

/// The body, refused as pydantic refused it when it is not an object or `field` is empty.
fn read<T: DeserializeOwned>(sent: &[u8], field: &str, list: bool) -> Result<T, ApiError> {
    let value = body::json(sent)?;
    let at = body::loc(&["body"]);
    body::object(&value, &at)?;
    let mut bounds = Bounds::new(&value, at.clone());
    if list {
        bounds.items(field)
    } else {
        bounds.text(field)
    };
    bounds.check()?;
    Ok(body::decode(&value, &at)?)
}

fn failed(what: &str, err: impl std::fmt::Display) -> ApiError {
    tracing::error!("{what} failed: {err}");
    ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
}

/// Questions to ask before researching, or none.
async fn clarify(
    State(app): State<Arc<App>>,
    sent: Bytes,
) -> Result<Json<ClarifyResponse>, ApiError> {
    let request: ClarifyRequest = read(&sent, "question", false)?;
    let settings = app.settings();
    settings.ready()?;
    let (role, tier, max_tokens) =
        *roles::RESEARCH.iter().find(|(role, ..)| *role == "clarify").expect("a research role");
    let target =
        roles::target(&settings, role, tier, max_tokens).map_err(|err| failed("clarify", err))?;
    let questions = kvasir_research::clarify::clarify(app.llm.as_ref(), &target, &request.question)
        .await
        .map_err(|err| failed("clarify", err))?;
    Ok(Json(ClarifyResponse { questions }))
}

/// One short completion on the fast model, for a client's own housekeeping.
///
/// Open WebUI generates a chat title, tags and follow-up prompts by asking whichever model the
/// chat is using, which for a kvasir chat is the pipe. Those are not research: sent to `/v1/runs`
/// they each take a run slot, and with one slot they collide with the very run they summarise.
/// What it spends is reported nowhere, since it belongs to no run.
async fn task(State(app): State<Arc<App>>, sent: Bytes) -> Result<Json<TaskResponse>, ApiError> {
    let request: TaskRequest = read(&sent, "messages", true)?;
    let settings = app.settings();
    settings.ready()?;
    let target = roles::task(&settings).map_err(|err| failed("a task", err))?;
    let field = |message: &std::collections::BTreeMap<String, String>, name: &str| {
        message.get(name).cloned().unwrap_or_default()
    };
    let messages: Vec<Message> = request
        .messages
        .iter()
        .map(|message| Message { role: field(message, "role"), content: field(message, "content") })
        .collect();
    let request =
        Request { target: &target, messages: &messages, json: false, retry_for: TASK_RETRY_FOR };
    let completion = app.llm.chat(request, &NoSink).await.map_err(|err| failed("a task", err))?;
    Ok(Json(TaskResponse { content: completion.content.unwrap_or_default() }))
}
