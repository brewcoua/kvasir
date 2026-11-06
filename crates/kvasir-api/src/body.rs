//! What a request sends, checked as FastAPI checked it, so a refusal has the same 422 body:
//! `{"detail": [{"type", "loc", "msg", "input", "ctx"}]}`, `loc` naming where the problem is from
//! `body`, `query` or `path` down.
//!
//! Types and unknown fields are serde's to find, and its errors are given pydantic's names. The
//! bounds pydantic's fields carried, a minimum length or value, are checked here on the JSON
//! before serde reads it.

use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

/// One problem with a request, as pydantic reported it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Detail {
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub loc: Vec<Value>,
    pub msg: String,
    pub input: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ctx: Option<Value>,
}

pub fn loc(parts: &[&str]) -> Vec<Value> {
    parts.iter().map(|&part| Value::from(part)).collect()
}

fn under(at: &[Value], name: &str) -> Vec<Value> {
    let mut loc = at.to_vec();
    loc.push(name.into());
    loc
}

pub fn missing(loc: Vec<Value>, input: Value) -> Detail {
    Detail { kind: "missing", loc, msg: "Field required".into(), input, ctx: None }
}

/// The body as JSON. An empty body is a missing one.
pub fn json(bytes: &[u8]) -> Result<Value, Vec<Detail>> {
    if bytes.iter().all(u8::is_ascii_whitespace) {
        return Err(vec![missing(loc(&["body"]), Value::Null)]);
    }
    serde_json::from_slice(bytes).map_err(|err| {
        // Python's json module counts characters from the start, where serde counts lines.
        let text = String::from_utf8_lossy(bytes);
        let before: usize =
            text.lines().take(err.line().saturating_sub(1)).map(|l| l.len() + 1).sum();
        let position = before + err.column().saturating_sub(1);
        vec![Detail {
            kind: "json_invalid",
            loc: vec!["body".into(), position.into()],
            msg: "JSON decode error".into(),
            input: json!({}),
            ctx: Some(json!({ "error": err.to_string() })),
        }]
    })
}

/// `value` as `T`, a problem located below `at`.
pub fn decode<T: DeserializeOwned>(value: &Value, at: &[Value]) -> Result<T, Vec<Detail>> {
    serde_path_to_error::deserialize(value).map_err(|err| {
        let mut loc = at.to_vec();
        let mut input = value;
        for segment in err.path().iter() {
            match segment {
                serde_path_to_error::Segment::Map { key } => {
                    input = input.get(key).unwrap_or(&Value::Null);
                    loc.push(key.as_str().into());
                }
                serde_path_to_error::Segment::Seq { index } => {
                    input = input.get(index).unwrap_or(&Value::Null);
                    loc.push((*index).into());
                }
                _ => {}
            }
        }
        vec![detail(&err.inner().to_string(), loc, input)]
    })
}

/// The words between backticks in a serde message.
fn quoted(message: &str) -> Vec<&str> {
    message.split('`').skip(1).step_by(2).collect()
}

fn detail(message: &str, mut loc: Vec<Value>, input: &Value) -> Detail {
    let named = quoted(message);
    if message.starts_with("missing field") {
        loc.push(named[0].into());
        return missing(loc, input.clone());
    }
    if message.starts_with("unknown field") {
        let field = named[0];
        // The path already ends at the field, so `input` is its value.
        let input = if loc.last() == Some(&Value::from(field)) {
            input.clone()
        } else {
            loc.push(field.into());
            input.get(field).cloned().unwrap_or(Value::Null)
        };
        return Detail {
            kind: "extra_forbidden",
            loc,
            msg: "Extra inputs are not permitted".into(),
            input,
            ctx: None,
        };
    }
    if message.starts_with("unknown variant") {
        let expected: Vec<String> = named[1..].iter().map(|name| format!("'{name}'")).collect();
        let listed = match expected.split_last() {
            Some((last, [])) => last.clone(),
            Some((last, rest)) => format!("{} or {last}", rest.join(", ")),
            None => String::new(),
        };
        return Detail {
            kind: "literal_error",
            loc,
            msg: format!("Input should be {listed}"),
            input: input.clone(),
            ctx: Some(json!({ "expected": listed })),
        };
    }
    let expected = message.rsplit_once(", expected ").map_or("", |(_, expected)| expected);
    let (kind, msg) = match expected {
        "u8" | "u16" | "u32" | "u64" | "usize" | "i8" | "i16" | "i32" | "i64" | "isize" => {
            ("int_type", "Input should be a valid integer".to_owned())
        }
        "f32" | "f64" => ("float_type", "Input should be a valid number".to_owned()),
        "a boolean" => ("bool_type", "Input should be a valid boolean".to_owned()),
        "a string" => ("string_type", "Input should be a valid string".to_owned()),
        "a sequence" => ("list_type", "Input should be a valid list".to_owned()),
        "a map" => ("dict_type", "Input should be a valid dictionary".to_owned()),
        _ => match expected.strip_prefix("struct ") {
            Some(name) => {
                ("model_type", format!("Input should be a valid dictionary or instance of {name}"))
            }
            None => ("value_error", message.to_owned()),
        },
    };
    Detail { kind, loc, msg, input: input.clone(), ctx: None }
}

/// `object` is a JSON object, as a model's fields are read from one.
pub fn object(value: &Value, at: &[Value]) -> Result<(), Vec<Detail>> {
    if value.is_object() {
        return Ok(());
    }
    Err(vec![Detail {
        kind: "model_attributes_type",
        loc: at.to_vec(),
        msg: "Input should be a valid dictionary or object to extract fields from".into(),
        input: value.clone(),
        ctx: None,
    }])
}

/// The bounds a model's fields hold, checked on the fields of `object` that are there.
pub struct Bounds<'a> {
    pub object: &'a Value,
    pub at: Vec<Value>,
    pub found: Vec<Detail>,
}

impl<'a> Bounds<'a> {
    pub fn new(object: &'a Value, at: Vec<Value>) -> Self {
        Self { object, at, found: Vec::new() }
    }

    /// A string field holds at least one character.
    pub fn text(&mut self, name: &str) -> &mut Self {
        if let Some(text) = self.object.get(name).and_then(Value::as_str)
            && text.is_empty()
        {
            self.found.push(Detail {
                kind: "string_too_short",
                loc: under(&self.at, name),
                msg: "String should have at least 1 character".into(),
                input: "".into(),
                ctx: Some(json!({ "min_length": 1 })),
            });
        }
        self
    }

    /// A list field holds at least one item.
    pub fn items(&mut self, name: &str) -> &mut Self {
        if let Some(items) = self.object.get(name).and_then(Value::as_array)
            && items.is_empty()
        {
            self.found.push(Detail {
                kind: "too_short",
                loc: under(&self.at, name),
                msg: "List should have at least 1 item after validation, not 0".into(),
                input: json!([]),
                ctx: Some(json!({ "field_type": "List", "min_length": 1, "actual_length": 0 })),
            });
        }
        self
    }

    /// A number field is at least `min`.
    pub fn at_least(&mut self, name: &str, min: i64) -> &mut Self {
        if let Some(value) =
            self.object.get(name).filter(|v| v.as_f64().is_some_and(|n| n < min as f64))
        {
            self.found.push(Detail {
                kind: "greater_than_equal",
                loc: under(&self.at, name),
                msg: format!("Input should be greater than or equal to {min}"),
                input: value.clone(),
                ctx: Some(json!({ "ge": min })),
            });
        }
        self
    }

    /// A number field is above `bound`.
    pub fn above(&mut self, name: &str, bound: i64) -> &mut Self {
        if let Some(value) =
            self.object.get(name).filter(|v| v.as_f64().is_some_and(|n| n <= bound as f64))
        {
            self.found.push(Detail {
                kind: "greater_than",
                loc: under(&self.at, name),
                msg: format!("Input should be greater than {bound}"),
                input: value.clone(),
                ctx: Some(json!({ "gt": bound })),
            });
        }
        self
    }

    pub fn check(&mut self) -> Result<(), Vec<Detail>> {
        if self.found.is_empty() { Ok(()) } else { Err(std::mem::take(&mut self.found)) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    #[allow(dead_code)]
    struct Options {
        max: Option<u64>,
        #[serde(default)]
        frontier: Choice,
        inner: Option<Inner>,
    }

    #[derive(Debug, Default, Deserialize)]
    #[serde(rename_all = "lowercase")]
    enum Choice {
        #[default]
        Breadth,
        Worth,
    }

    #[derive(Debug, Deserialize)]
    #[allow(dead_code)]
    struct Inner {
        name: String,
    }

    fn first(value: Value) -> Detail {
        decode::<Options>(&value, &loc(&["body", "options"])).unwrap_err().remove(0)
    }

    #[test]
    fn serde_errors_have_pydantic_names_and_places() {
        let extra = first(json!({"nope": 1}));
        assert_eq!((extra.kind, extra.loc), ("extra_forbidden", loc(&["body", "options", "nope"])));
        assert_eq!(extra.input, json!(1));

        let typed = first(json!({"max": "many"}));
        assert_eq!((typed.kind, typed.loc), ("int_type", loc(&["body", "options", "max"])));
        assert_eq!(typed.input, json!("many"));

        let choice = first(json!({"frontier": "deep"}));
        assert_eq!(choice.kind, "literal_error");
        assert_eq!(choice.msg, "Input should be 'breadth' or 'worth'");

        let absent = first(json!({"inner": {}}));
        assert_eq!(
            (absent.kind, absent.loc),
            ("missing", loc(&["body", "options", "inner", "name"]))
        );
    }

    #[test]
    fn bounds_are_checked_on_the_fields_given() {
        let value = json!({"question": "", "max": 0, "minutes": 0, "messages": []});
        let found = Bounds::new(&value, loc(&["body"]))
            .text("question")
            .at_least("max", 1)
            .at_least("absent", 1)
            .above("minutes", 0)
            .items("messages")
            .check()
            .unwrap_err();
        let kinds: Vec<&str> = found.iter().map(|d| d.kind).collect();
        assert_eq!(kinds, ["string_too_short", "greater_than_equal", "greater_than", "too_short"]);
        assert_eq!(found[1].msg, "Input should be greater than or equal to 1");
    }

    #[test]
    fn a_body_that_is_not_json_or_is_empty_is_refused() {
        assert_eq!(json(b"").unwrap_err()[0].kind, "missing");
        let bad = json(b"{\"a\": }").unwrap_err().remove(0);
        assert_eq!((bad.kind, bad.loc), ("json_invalid", vec![json!("body"), json!(6)]));
    }
}
