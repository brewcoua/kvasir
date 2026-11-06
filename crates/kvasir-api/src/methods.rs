//! What can be run: one closed enum, so a method left out of a match is a compile error. Adding a
//! method is a crate, a variant, a match arm in each function here, and its options' fields.
//!
//! The options' JSON Schema is written out field by field, as pydantic wrote it, since the runs page
//! and `tests/contract/shapes.json` read pydantic's spelling: `anyOf` with `null` for an optional
//! field, a `title` per field, `minimum` for a bound. The same fields give the bounds a request is
//! checked against.

use std::sync::Arc;

use axum::Json;
use axum::extract::State;
use axum::routing::get;
use kvasir_config::Settings;
use kvasir_research::{Classify, Scoped, Tools};
use kvasir_run::RunCtx;
use kvasir_run::stages::{ARTICLE, OUTLINE, POLISH, RESEARCH};
use kvasir_sites::sources::Policy;
use kvasir_types::requests::{
    GraphOptions, Input, LedgerOptions, MethodInfo, RunRequest, StormOptions,
};
use kvasir_types::results::RunResult;
use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Map, Value, json};

use crate::body::{self, Bounds, Detail};
use crate::{App, Live, settings};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Method {
    Ledger,
    Graph,
    Storm,
}

/// What an option holds, and the value it has when a request leaves it out.
#[derive(Clone, Copy)]
enum Kind {
    /// A whole number of at least this, or null.
    Count(i64),
    /// Minutes above 0.
    Minutes(f64),
    Bool(bool),
    OptionalBool,
    OptionalText,
    OptionalList,
    /// One of these, the first by default.
    Choice(&'static [&'static str]),
    OptionalChoice(&'static [&'static str]),
}

use Kind::{
    Bool, Choice, Count, Minutes, OptionalBool, OptionalChoice, OptionalList, OptionalText,
};

const OUTPUTS: &[&str] = &["report", "ranking", "table"];

const LEDGER: &[(&str, Kind)] = &[
    ("output", OptionalChoice(OUTPUTS)),
    ("criteria", OptionalList),
    ("entities", OptionalList),
    ("max_searches", Count(1)),
    ("results_per_query", Count(1)),
    ("max_attempts", Count(1)),
    ("follow_ups", OptionalBool),
    ("max_depth", Count(1)),
    ("max_entities", Count(1)),
    ("max_criteria", Count(1)),
    ("frontier", Choice(&["breadth", "worth"])),
    ("ground", OptionalBool),
    ("ground_intake", Bool(false)),
    ("corroborate", Bool(false)),
    ("contradict", Bool(false)),
    ("recall_past_runs", Bool(true)),
    ("max_age_hours", Count(0)),
    ("model_fast", OptionalText),
    ("model_strong", OptionalText),
    ("model_reason", OptionalText),
    ("lists_on", OptionalList),
    ("lists_off", OptionalList),
];

const GRAPH: &[(&str, Kind)] = &[
    ("research_minutes", Minutes(20.0)),
    ("max_searches", Count(1)),
    ("results_per_query", Count(1)),
    ("max_attempts", Count(1)),
    ("max_depth", Count(1)),
    ("ground", OptionalBool),
    ("ground_intake", Bool(false)),
    ("corroborate", Bool(false)),
    ("contradict", Bool(false)),
    ("recall_past_runs", Bool(true)),
    ("max_age_hours", Count(0)),
    ("model_fast", OptionalText),
    ("model_strong", OptionalText),
    ("model_reason", OptionalText),
    ("lists_on", OptionalList),
    ("lists_off", OptionalList),
];

const STORM: &[(&str, Kind)] = &[
    ("search_top_k", Count(1)),
    ("max_conv_turn", Count(1)),
    ("max_perspective", Count(1)),
    ("do_polish_article", Bool(true)),
    ("model_fast", OptionalText),
    ("model_strong", OptionalText),
    ("lists_on", OptionalList),
    ("lists_off", OptionalList),
];

impl Method {
    pub const ALL: [Self; 3] = [Self::Ledger, Self::Graph, Self::Storm];

    pub fn name(self) -> &'static str {
        match self {
            Self::Ledger => "ledger",
            Self::Graph => "graph",
            Self::Storm => "storm",
        }
    }

    /// Whether a client should ask `/v1/clarify` before starting a run.
    fn clarifies(self) -> bool {
        !matches!(self, Self::Storm)
    }

    fn fields(self) -> &'static [(&'static str, Kind)] {
        match self {
            Self::Ledger => LEDGER,
            Self::Graph => GRAPH,
            Self::Storm => STORM,
        }
    }

    pub fn options_name(self) -> &'static str {
        match self {
            Self::Ledger => "LedgerOptions",
            Self::Graph => "GraphOptions",
            Self::Storm => "StormOptions",
        }
    }

    fn options_description(self) -> &'static str {
        match self {
            Self::Ledger => {
                "Options of the `ledger` method. The budgets fall back to the configured defaults."
            }
            Self::Graph => {
                "Options of the `graph` method. The budgets fall back to the configured defaults."
            }
            Self::Storm => {
                "Options of the `storm` method. Each falls back to the configured default when \
                 omitted."
            }
        }
    }

    /// The options' JSON Schema, with each field's declared default.
    pub fn schema(self) -> Map<String, Value> {
        let properties: Map<String, Value> = self
            .fields()
            .iter()
            .map(|(name, kind)| ((*name).to_owned(), field(name, *kind)))
            .collect();
        let schema = json!({
            "additionalProperties": false,
            "description": self.options_description(),
            "properties": properties,
            "title": self.options_name(),
            "type": "object",
        });
        let Value::Object(schema) = schema else { unreachable!("an object") };
        schema
    }

    /// What each option a request leaves out resolves to, as configured.
    fn defaults(self, settings: &Settings) -> Value {
        let model = &settings.model;
        let reason = model.reason.clone().unwrap_or_else(|| model.strong.clone());
        let research = &settings.research;
        match self {
            Self::Ledger => json!({
                "max_searches": research.max_searches,
                "results_per_query": research.results_per_query,
                "max_attempts": research.max_attempts,
                "follow_ups": research.follow_ups,
                "max_depth": research.max_depth,
                "max_entities": research.max_entities,
                "max_criteria": research.max_criteria,
                "max_age_hours": settings.fetch.max_age_hours,
                "ground": model.checker.is_some(),
                "model_fast": model.fast,
                "model_strong": model.strong,
                "model_reason": reason,
            }),
            Self::Graph => json!({
                "results_per_query": kvasir_graph::RESULTS_PER_QUERY,
                "max_attempts": research.max_attempts,
                "max_depth": kvasir_graph::MAX_DEPTH,
                "max_age_hours": settings.fetch.max_age_hours,
                "ground": model.checker.is_some(),
                "model_fast": model.fast,
                "model_strong": model.strong,
                "model_reason": reason,
            }),
            Self::Storm => json!({
                "search_top_k": settings.search.top_k,
                "max_conv_turn": settings.storm.max_conv_turn,
                "max_perspective": settings.storm.max_perspective,
                "model_fast": model.fast,
                "model_strong": model.strong,
            }),
        }
    }

    pub fn info(self, settings: &Settings) -> MethodInfo {
        let mut options = self.schema();
        let properties = options["properties"].as_object_mut().expect("an object");
        let Value::Object(defaults) = self.defaults(settings) else { unreachable!("an object") };
        for (name, value) in defaults {
            properties[&name]["default"] = value;
        }
        let defaults = match self {
            Self::Ledger => {
                RunRequest::Ledger { input: blank(), options: LedgerOptions::default() }
            }
            Self::Graph => RunRequest::Graph { input: blank(), options: GraphOptions::default() },
            Self::Storm => RunRequest::Storm { input: blank(), options: StormOptions::default() },
        };
        MethodInfo {
            name: self.name().into(),
            clarifies: self.clarifies(),
            stages: stages(&defaults),
            options,
        }
    }
}

fn blank() -> Input {
    Input { question: String::new(), clarifications: Vec::new() }
}

/// pydantic's title for a field: its words, each capitalized.
fn title(name: &str) -> String {
    name.split('_')
        .map(|word| {
            let mut chars = word.chars();
            chars
                .next()
                .map_or_else(String::new, |first| first.to_uppercase().chain(chars).collect())
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn field(name: &str, kind: Kind) -> Value {
    let nullable =
        |schema: Value| json!({ "anyOf": [schema, { "type": "null" }], "default": null });
    let mut schema = match kind {
        Count(min) => nullable(json!({ "minimum": min, "type": "integer" })),
        Minutes(default) => json!({ "default": default, "exclusiveMinimum": 0, "type": "number" }),
        Bool(default) => json!({ "default": default, "type": "boolean" }),
        OptionalBool => nullable(json!({ "type": "boolean" })),
        OptionalText => nullable(json!({ "type": "string" })),
        OptionalList => nullable(json!({ "items": { "type": "string" }, "type": "array" })),
        Choice(choices) => json!({ "default": choices[0], "enum": choices, "type": "string" }),
        OptionalChoice(choices) => nullable(json!({ "enum": choices, "type": "string" })),
    };
    schema["title"] = title(name).into();
    schema
}

/// The stages a run with these options plans to go through, for the runs page to show ahead.
pub fn stages(request: &RunRequest) -> Vec<String> {
    let names: Vec<&str> = match request {
        RunRequest::Ledger { options, .. } => kvasir_ledger::stages(options),
        RunRequest::Graph { .. } => kvasir_graph::STAGES.to_vec(),
        RunRequest::Storm { options, .. } => {
            let mut names = vec![RESEARCH, OUTLINE, ARTICLE];
            if options.do_polish_article {
                names.push(POLISH);
            }
            names
        }
    };
    names.into_iter().map(Into::into).collect()
}

pub fn method(request: &RunRequest) -> Method {
    match request {
        RunRequest::Ledger { .. } => Method::Ledger,
        RunRequest::Graph { .. } => Method::Graph,
        RunRequest::Storm { .. } => Method::Storm,
    }
}

pub fn input(request: &RunRequest) -> &Input {
    match request {
        RunRequest::Ledger { input, .. }
        | RunRequest::Graph { input, .. }
        | RunRequest::Storm { input, .. } => input,
    }
}

/// The source lists this run turns on and off for itself.
pub fn policy(request: &RunRequest) -> Policy {
    let (on, off) = match request {
        RunRequest::Ledger { options: o, .. } => (&o.lists_on, &o.lists_off),
        RunRequest::Graph { options: o, .. } => (&o.lists_on, &o.lists_off),
        RunRequest::Storm { options: o, .. } => (&o.lists_on, &o.lists_off),
    };
    let ids = |ids: &Option<Vec<String>>| ids.iter().flatten().cloned().collect();
    Policy { on: ids(on), off: ids(off) }
}

/// The per-request models, fast, strong and reason, in that order.
pub fn models(request: &RunRequest) -> [Option<&str>; 3] {
    match request {
        RunRequest::Ledger { options: o, .. } => {
            [o.model_fast.as_deref(), o.model_strong.as_deref(), o.model_reason.as_deref()]
        }
        RunRequest::Graph { options: o, .. } => {
            [o.model_fast.as_deref(), o.model_strong.as_deref(), o.model_reason.as_deref()]
        }
        RunRequest::Storm { options: o, .. } => {
            [o.model_fast.as_deref(), o.model_strong.as_deref(), None]
        }
    }
}

#[derive(Deserialize)]
struct Run<O> {
    input: Input,
    #[serde(default)]
    options: O,
}

fn variant<O: DeserializeOwned + Default>(
    value: &Value,
    at: &[Value],
) -> Result<Run<O>, Vec<Detail>> {
    body::decode(value, at)
}

/// The body of `POST /v1/runs`, refused as pydantic refused it: by the `method` tag first, then
/// below the tag's name, as `["body", "ledger", "options", "nope"]`.
pub fn request(value: &Value) -> Result<RunRequest, Vec<Detail>> {
    let at = body::loc(&["body"]);
    body::object(value, &at)?;
    let tags = "'ledger', 'graph', 'storm'";
    let Some(tag) = value.get("method").and_then(Value::as_str) else {
        return Err(vec![Detail {
            kind: "union_tag_not_found",
            loc: at,
            msg: "Unable to extract tag using discriminator 'method'".into(),
            input: value.clone(),
            ctx: Some(json!({ "discriminator": "'method'" })),
        }]);
    };
    let Some(method) = Method::ALL.into_iter().find(|m| m.name() == tag) else {
        return Err(vec![Detail {
            kind: "union_tag_invalid",
            loc: at,
            msg: format!(
                "Input tag '{tag}' found using 'method' does not match any of the expected \
                 tags: {tags}"
            ),
            input: value.clone(),
            ctx: Some(json!({
                "discriminator": "'method'",
                "tag": format!("'{tag}'"),
                "expected_tags": tags,
            })),
        }]);
    };
    let at = body::loc(&["body", tag]);
    let mut found = Vec::new();
    if let Some(input) = value.get("input") {
        let mut loc = at.clone();
        loc.push("input".into());
        found.extend(Bounds::new(input, loc).text("question").check().err().unwrap_or_default());
    }
    if let Some(options) = value.get("options") {
        let mut loc = at.clone();
        loc.push("options".into());
        let mut bounds = Bounds::new(options, loc);
        for (name, kind) in method.fields() {
            match kind {
                Count(min) => bounds.at_least(name, *min),
                Minutes(_) => bounds.above(name, 0),
                _ => &mut bounds,
            };
        }
        found.extend(bounds.check().err().unwrap_or_default());
    }
    if !found.is_empty() {
        return Err(found);
    }
    Ok(match method {
        Method::Ledger => {
            let Run { input, options } = variant(value, &at)?;
            RunRequest::Ledger { input, options }
        }
        Method::Graph => {
            let Run { input, options } = variant(value, &at)?;
            RunRequest::Graph { input, options }
        }
        Method::Storm => {
            let Run { input, options } = variant(value, &at)?;
            RunRequest::Storm { input, options }
        }
    })
}

/// Why a run ended without a result, named as the frame and the record give it.
#[derive(Debug, thiserror::Error)]
pub enum RunError {
    #[error("ConfigError: {0}")]
    Config(#[from] kvasir_config::ConfigError),
    #[error("ResearchError: {0}")]
    Research(#[from] kvasir_research::ResearchError),
    #[error("StormError: {0}")]
    Storm(#[from] kvasir_storm::StormError),
}

/// Runs `request` to its result with the settings and services of `live`, reporting into `ctx`.
pub async fn run(
    app: &App,
    live: &Live,
    request: &RunRequest,
    ctx: &RunCtx,
) -> Result<RunResult, RunError> {
    let settings = &live.settings;
    let today = kvasir_research::today();
    let [fast, strong, reason] = models(request);
    let policy = policy(request);
    let scoped: Option<Arc<dyn Classify>> = match &live.sources {
        Some(sources) if !policy.is_empty() => {
            sources.prepare(&policy).await;
            Some(Arc::new(Scoped { sources: Arc::clone(sources), policy }))
        }
        _ => None,
    };
    let scope = |mut tools: Tools| {
        if scoped.is_some() {
            tools.sources.clone_from(&scoped);
        }
        tools
    };
    let tools = |ground: Option<bool>,
                 ground_intake: bool,
                 max_age: Option<u64>|
     -> Result<Tools, RunError> {
        let targets = kvasir_research::targets(
            settings,
            fast,
            strong,
            reason,
            ground.unwrap_or(true),
            ground_intake,
        )?;
        Ok(scope(settings::tools(app, live, targets, max_age)?))
    };
    match request {
        RunRequest::Ledger { input, options } => {
            let tools = tools(options.ground, options.ground_intake, options.max_age_hours)?;
            let result =
                kvasir_ledger::research(&tools, &settings.research, ctx, input, options, &today);
            Ok(RunResult::Research(result.await?))
        }
        RunRequest::Graph { input, options } => {
            let tools = tools(options.ground, options.ground_intake, options.max_age_hours)?;
            let result =
                kvasir_graph::research(&tools, &settings.research, ctx, input, options, &today);
            Ok(RunResult::Research(result.await?))
        }
        RunRequest::Storm { input, options } => {
            let targets = kvasir_storm::targets(settings, fast, strong)?;
            let tools = scope(settings::tools(app, live, targets, None)?);
            let result =
                kvasir_storm::run(&tools, &app.web, &settings.storm, ctx, input, options).await?;
            Ok(RunResult::Article(result))
        }
    }
}

pub fn routes() -> axum::Router<Arc<App>> {
    axum::Router::new().route("/v1/methods", get(list))
}

/// Every method, with its options and their configured defaults, for a client to show.
async fn list(State(app): State<Arc<App>>) -> Json<Vec<MethodInfo>> {
    let settings = app.settings();
    Json(Method::ALL.into_iter().map(|method| method.info(&settings)).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_field_is_spelled_as_pydantic_spelled_it() {
        assert_eq!(
            field("max_searches", Count(1)),
            json!({
                "anyOf": [{ "minimum": 1, "type": "integer" }, { "type": "null" }],
                "default": null,
                "title": "Max Searches",
            })
        );
        assert_eq!(
            field("frontier", Choice(&["breadth", "worth"])),
            json!({ "default": "breadth", "enum": ["breadth", "worth"], "type": "string", "title": "Frontier" })
        );
    }

    #[test]
    fn every_option_a_type_reads_has_a_field() {
        for (method, options) in [
            (Method::Ledger, serde_json::to_value(LedgerOptions::default()).unwrap()),
            (Method::Graph, serde_json::to_value(GraphOptions::default()).unwrap()),
            (Method::Storm, serde_json::to_value(StormOptions::default()).unwrap()),
        ] {
            let names: Vec<&str> = method.fields().iter().map(|(name, _)| *name).collect();
            let read: Vec<&str> = options.as_object().unwrap().keys().map(String::as_str).collect();
            assert_eq!(names, read, "{}", method.name());
        }
    }

    #[test]
    fn a_request_is_refused_where_pydantic_refused_it() {
        let refused = |value: Value| request(&value).unwrap_err().remove(0);
        let kinds = [
            (json!({}), "union_tag_not_found", vec!["body"]),
            (
                json!({"method": "nope", "input": {"question": "x"}}),
                "union_tag_invalid",
                vec!["body"],
            ),
            (
                json!({"method": "ledger", "input": {"question": "x"}, "options": {"nope": 1}}),
                "extra_forbidden",
                vec!["body", "ledger", "options", "nope"],
            ),
            (
                json!({"method": "ledger", "input": {"question": ""}}),
                "string_too_short",
                vec!["body", "ledger", "input", "question"],
            ),
            (
                json!({"method": "graph", "input": {"question": "x"}, "options": {"research_minutes": 0}}),
                "greater_than",
                vec!["body", "graph", "options", "research_minutes"],
            ),
            (
                json!({"method": "ledger", "input": {"question": "x"}, "options": {"max_searches": 0}}),
                "greater_than_equal",
                vec!["body", "ledger", "options", "max_searches"],
            ),
            (json!({"method": "storm"}), "missing", vec!["body", "storm", "input"]),
        ];
        for (value, kind, loc) in kinds {
            let found = refused(value);
            assert_eq!((found.kind, found.loc), (kind, body::loc(&loc)));
        }
        let ok = request(&json!({"method": "graph", "input": {"question": "x"}})).unwrap();
        assert_eq!(stages(&ok), ["brief", "research", "write", "verify"]);
    }
}
