//! The OpenAPI document the web client's types are generated from: `kvasir openapi`.
//!
//! Only `components` is filled. The client imports schemas by name and nothing from `paths`, so
//! the document lists every schema a route takes or answers and no route. The methods' options and
//! the run bodies are written as pydantic wrote them, which `/v1/methods` must match.

use std::collections::BTreeMap;

use schemars::generate::SchemaSettings;
use schemars::{JsonSchema, SchemaGenerator};
use serde_json::{Value, json};

use crate::methods::Method;
use crate::providers::{ModelInfo, ProviderInfo};
use crate::settings::{SettingInfo, SettingsView};
use crate::{auth, sources};
use kvasir_types::{pages, requests, results, runs, sites};

/// `T` and every schema it refers to.
fn add<T: JsonSchema>(generator: &mut SchemaGenerator) {
    generator.subschema_for::<T>();
}

/// The fields FastAPI's schemas left optional, by schema. Every other property is required, as a
/// field with a default is always sent.
const OPTIONAL: &[(&str, &[&str])] = &[
    ("Cell", &["rank"]),
    ("Citation", &["source"]),
    ("LmCall", &["id", "event"]),
    ("LmCallContent", &["reasoning"]),
    ("ModelInfo", &["context_length", "capabilities"]),
    (
        "Progress",
        &[
            "at",
            "done",
            "total",
            "seq",
            "kind",
            "parent",
            "question",
            "search",
            "page",
            "series",
            "updated_at",
        ],
    ),
    ("Row", &["rank"]),
    ("SearchResult", &["source"]),
    ("SettingInfo", &["minimum", "choices"]),
    ("Site", &["source"]),
    ("SiteNode", &["source"]),
];

/// No `default`, as FastAPI's response schemas have none: `required` alone says what is sent. One
/// default is the time the document was made.
fn undefault(schema: &mut Value) {
    match schema {
        Value::Object(object) => {
            object.remove("default");
            object.values_mut().for_each(undefault);
        }
        Value::Array(items) => items.iter_mut().for_each(undefault),
        _ => {}
    }
}

fn require(name: &str, schema: &mut Value) {
    let optional = OPTIONAL.iter().find(|(schema, _)| *schema == name).map_or(&[][..], |(_, f)| *f);
    let Value::Object(object) = schema else { return };
    let Some(Value::Object(properties)) = object.get("properties") else { return };
    let required: Vec<Value> = properties
        .keys()
        .filter(|key| !optional.contains(&key.as_str()))
        .cloned()
        .map(Value::from)
        .collect();
    object.insert("required".into(), Value::Array(required));
}

/// A run body: `method` names the method, and its options are the method's own.
fn run(method: Method) -> Value {
    json!({
        "type": "object",
        "title": format!("{}Run", title(method.name())),
        "properties": {
            "method": { "type": "string", "const": method.name(), "title": "Method" },
            "input": { "$ref": "#/components/schemas/Input" },
            "options": { "$ref": format!("#/components/schemas/{}", method.options_name()) },
        },
        "required": ["method", "input"],
    })
}

fn title(name: &str) -> String {
    let mut chars = name.chars();
    chars.next().map(|first| first.to_uppercase().chain(chars).collect()).unwrap_or_default()
}

pub fn document() -> Value {
    let settings = SchemaSettings::draft2020_12().with(|settings| {
        settings.definitions_path = "/components/schemas".into();
    });
    let mut generator = SchemaGenerator::new(settings);
    add::<pages::CacheStats>(&mut generator);
    add::<pages::PageDetail>(&mut generator);
    add::<pages::PageSummary>(&mut generator);
    add::<pages::Recall>(&mut generator);
    add::<pages::Site>(&mut generator);
    add::<pages::SiteGraph>(&mut generator);
    add::<requests::ClarifyRequest>(&mut generator);
    add::<requests::ClarifyResponse>(&mut generator);
    add::<requests::Input>(&mut generator);
    add::<requests::MethodInfo>(&mut generator);
    add::<requests::TaskRequest>(&mut generator);
    add::<requests::TaskResponse>(&mut generator);
    add::<results::ArticleResult>(&mut generator);
    add::<results::ResearchResult>(&mut generator);
    add::<runs::LmCallContent>(&mut generator);
    add::<runs::RunDetail>(&mut generator);
    add::<runs::RunSummary>(&mut generator);
    add::<runs::SearchContent>(&mut generator);
    add::<sites::SourceInfo>(&mut generator);
    add::<SettingsView>(&mut generator);
    add::<SettingInfo>(&mut generator);
    add::<ProviderInfo>(&mut generator);
    add::<ModelInfo>(&mut generator);
    add::<auth::Login>(&mut generator);
    add::<sources::Mark>(&mut generator);

    // By name, as FastAPI listed them, so the generated file reads in order.
    let mut schemas: BTreeMap<String, Value> =
        generator.take_definitions(true).into_iter().collect();
    for (name, schema) in &mut schemas {
        undefault(schema);
        require(name, schema);
    }
    for method in Method::ALL {
        schemas.insert(method.options_name().into(), Value::Object(method.schema()));
        schemas.insert(format!("{}Run", title(method.name())), run(method));
    }
    schemas.insert(
        "ValidationError".into(),
        json!({
            "type": "object",
            "title": "ValidationError",
            "properties": {
                "loc": {
                    "type": "array",
                    "title": "Location",
                    "items": { "anyOf": [{ "type": "string" }, { "type": "integer" }] },
                },
                "msg": { "type": "string", "title": "Message" },
                "type": { "type": "string", "title": "Error Type" },
                "input": { "title": "Input" },
                "ctx": { "type": "object", "title": "Context" },
            },
            "required": ["loc", "msg", "type"],
        }),
    );
    schemas.insert(
        "HTTPValidationError".into(),
        json!({
            "type": "object",
            "title": "HTTPValidationError",
            "properties": {
                "detail": {
                    "type": "array",
                    "title": "Detail",
                    "items": { "$ref": "#/components/schemas/ValidationError" },
                },
            },
        }),
    );
    json!({
        "openapi": "3.1.0",
        "info": { "title": "kvasir", "version": env!("CARGO_PKG_VERSION") },
        "paths": {},
        "components": { "schemas": schemas },
    })
}
