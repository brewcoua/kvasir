//! How a field's type is described to the model and read back from its reply, as dspy and pydantic
//! do it: the name dspy prints, pydantic's JSON Schema, and pydantic's lax coercion.

use serde_json::{Map, Value, json};

/// How dspy renders and parses a field of this type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Str,
    Bool,
    Int,
    Float,
    /// One of these strings, exactly.
    Literal(&'static [&'static str]),
    /// Anything else, given to the model as a JSON Schema.
    Schema,
}

/// A type a signature field can have.
pub trait Shape {
    /// As dspy's `get_annotation_name` writes it: `str`, `list[int]`, `Literal['a', 'b']`.
    fn type_name() -> String;
    fn kind() -> Kind {
        Kind::Schema
    }
    /// pydantic's JSON Schema for the type, with models put in `defs` and referenced.
    fn schema(defs: &mut Map<String, Value>) -> Value;
    /// `value` as pydantic's lax mode accepts it, or `None` when it does not.
    fn lax(value: &Value) -> Option<Value>;
}

impl Shape for String {
    fn type_name() -> String {
        "str".into()
    }
    fn kind() -> Kind {
        Kind::Str
    }
    fn schema(_: &mut Map<String, Value>) -> Value {
        json!({"type": "string"})
    }
    fn lax(value: &Value) -> Option<Value> {
        value.is_string().then(|| value.clone())
    }
}

impl Shape for bool {
    fn type_name() -> String {
        "bool".into()
    }
    fn kind() -> Kind {
        Kind::Bool
    }
    fn schema(_: &mut Map<String, Value>) -> Value {
        json!({"type": "boolean"})
    }
    fn lax(value: &Value) -> Option<Value> {
        match value {
            Value::Bool(_) => Some(value.clone()),
            Value::Number(n) if n.as_f64() == Some(1.0) => Some(true.into()),
            Value::Number(n) if n.as_f64() == Some(0.0) => Some(false.into()),
            Value::String(s) => match s.to_lowercase().as_str() {
                "true" | "t" | "yes" | "y" | "on" | "1" => Some(true.into()),
                "false" | "f" | "no" | "n" | "off" | "0" => Some(false.into()),
                _ => None,
            },
            _ => None,
        }
    }
}

impl Shape for i64 {
    fn type_name() -> String {
        "int".into()
    }
    fn kind() -> Kind {
        Kind::Int
    }
    fn schema(_: &mut Map<String, Value>) -> Value {
        json!({"type": "integer"})
    }
    fn lax(value: &Value) -> Option<Value> {
        let whole = |f: f64| (f.fract() == 0.0 && f.is_finite()).then(|| json!(f as i64));
        match value {
            Value::Number(n) if n.is_i64() || n.is_u64() => Some(value.clone()),
            Value::Number(n) => whole(n.as_f64()?),
            Value::Bool(b) => Some(json!(i64::from(*b))),
            Value::String(s) => {
                let s = s.trim();
                s.parse::<i64>().ok().map(Value::from).or_else(|| whole(s.parse().ok()?))
            }
            _ => None,
        }
    }
}

impl Shape for f64 {
    fn type_name() -> String {
        "float".into()
    }
    fn kind() -> Kind {
        Kind::Float
    }
    fn schema(_: &mut Map<String, Value>) -> Value {
        json!({"type": "number"})
    }
    fn lax(value: &Value) -> Option<Value> {
        match value {
            Value::Number(_) => Some(value.clone()),
            Value::Bool(b) => Some(json!(f64::from(u8::from(*b)))),
            Value::String(s) => s.trim().parse::<f64>().ok().map(Value::from),
            _ => None,
        }
    }
}

impl<T: Shape> Shape for Vec<T> {
    fn type_name() -> String {
        format!("list[{}]", T::type_name())
    }
    fn schema(defs: &mut Map<String, Value>) -> Value {
        json!({"items": T::schema(defs), "type": "array"})
    }
    fn lax(value: &Value) -> Option<Value> {
        value.as_array()?.iter().map(T::lax).collect::<Option<Vec<_>>>().map(Value::Array)
    }
}

/// `Literal['a', 'b']`, quoted as dspy quotes a literal's strings.
pub fn literal_name(values: &[&str]) -> String {
    let quoted: Vec<String> = values.iter().map(|v| format!("'{v}'")).collect();
    format!("Literal[{}]", quoted.join(", "))
}

/// `value` if it is one of `values`, as a `Literal` of strings reads it.
pub fn exact(values: &[&str], value: &Value) -> Option<Value> {
    values.iter().any(|v| value == *v).then(|| value.clone())
}

/// pydantic's schema for a `Literal` of strings.
pub fn literal_schema(values: &[&str]) -> Value {
    json!({"enum": values, "type": "string"})
}

/// One property of a model, for [`model_schema`] and [`model_lax`].
pub struct Prop {
    pub name: &'static str,
    pub description: Option<&'static str>,
    /// Its value when the reply leaves it out. `None` makes it required.
    pub default: Option<Value>,
    /// Whether the schema shows the default, as it does for pydantic's `default` and not for its
    /// `default_factory`.
    pub default_shown: bool,
    pub schema: fn(&mut Map<String, Value>) -> Value,
    pub lax: fn(&Value) -> Option<Value>,
}

impl Prop {
    pub fn of<T: Shape>(name: &'static str) -> Self {
        Self {
            name,
            description: None,
            default: None,
            default_shown: false,
            schema: T::schema,
            lax: T::lax,
        }
    }

    pub fn describe(mut self, description: &'static str) -> Self {
        self.description = Some(description);
        self
    }

    pub fn default(mut self, value: Value) -> Self {
        self.default = Some(value);
        self.default_shown = true;
        self
    }

    /// A default the schema does not show, as pydantic's `default_factory`.
    pub fn default_factory(mut self, value: Value) -> Self {
        self.default = Some(value);
        self.default_shown = false;
        self
    }

    /// Reads the property with `lax` in place of its type's, for a pydantic `mode="before"`
    /// validator.
    pub fn lax(mut self, lax: fn(&Value) -> Option<Value>) -> Self {
        self.lax = lax;
        self
    }
}

/// pydantic's schema for a model named `title`: put in `defs`, with a reference returned.
pub fn model_schema(
    defs: &mut Map<String, Value>,
    title: &str,
    description: Option<&str>,
    props: &[Prop],
) -> Value {
    let mut properties = Map::new();
    for prop in props {
        let mut schema = (prop.schema)(defs);
        let object = schema.as_object_mut().expect("schemas are objects");
        if let Some(default) = prop.default.as_ref().filter(|_| prop.default_shown) {
            object.insert("default".into(), default.clone());
        }
        if let Some(description) = prop.description {
            object.insert("description".into(), description.into());
        }
        object.insert("title".into(), field_title(prop.name).into());
        properties.insert(prop.name.into(), schema);
    }
    let required: Vec<&str> =
        props.iter().filter(|p| p.default.is_none()).map(|p| p.name).collect();
    let mut model = json!({"properties": properties, "title": title, "type": "object"});
    if !required.is_empty() {
        model["required"] = required.into();
    }
    if let Some(description) = description {
        model["description"] = description.into();
    }
    defs.insert(title.into(), model);
    json!({"$ref": format!("#/$defs/{title}")})
}

/// A model's properties read laxly. A property the model does not declare is dropped.
pub fn model_lax(value: &Value, props: &[Prop]) -> Option<Value> {
    let object = value.as_object()?;
    let mut out = Map::new();
    for prop in props {
        let value = match (object.get(prop.name), &prop.default) {
            (Some(v), _) => (prop.lax)(v)?,
            (None, Some(default)) => default.clone(),
            (None, None) => return None,
        };
        out.insert(prop.name.into(), value);
    }
    Some(Value::Object(out))
}

/// pydantic's title for a field: `claim_ids` is `Claim Ids`.
fn field_title(name: &str) -> String {
    name.split('_')
        .map(|word| {
            let mut chars = word.chars();
            chars.next().map_or_else(String::new, |c| c.to_uppercase().chain(chars).collect())
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// The schema of `T` as pydantic writes it for a whole type, with `$defs` when there are models.
pub fn root_schema<T: Shape>() -> Value {
    let mut defs = Map::new();
    let mut schema = T::schema(&mut defs);
    if !defs.is_empty() {
        schema["$defs"] = Value::Object(defs);
    }
    schema
}

/// The text dspy puts after "must adhere to the JSON schema: ": `type` first, then keys sorted, at
/// every level, written as Python's `json.dumps` writes.
pub fn schema_text(schema: &Value) -> String {
    let mut out = String::new();
    dump(&type_first(schema), &mut out);
    out
}

fn type_first(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort_by_key(|k| (k.as_str() != "type", k.as_str()));
            Value::Object(keys.into_iter().map(|k| (k.clone(), type_first(&map[k]))).collect())
        }
        Value::Array(items) => Value::Array(items.iter().map(type_first).collect()),
        other => other.clone(),
    }
}

/// Python's `json.dumps(value, ensure_ascii=False)`: `, ` and `: ` between items.
pub fn dump(value: &Value, out: &mut String) {
    match value {
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                dump(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, (k, v)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                out.push_str(&Value::from(k.as_str()).to_string());
                out.push_str(": ");
                dump(v, out);
            }
            out.push('}');
        }
        other => out.push_str(&other.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn titles() {
        assert_eq!(field_title("claim_ids"), "Claim Ids");
        assert_eq!(field_title("title"), "Title");
    }

    #[test]
    fn lax_values() {
        assert_eq!(i64::lax(&json!("3")), Some(json!(3)));
        assert_eq!(i64::lax(&json!(3.0)), Some(json!(3)));
        assert_eq!(i64::lax(&json!(3.5)), None);
        assert_eq!(bool::lax(&json!("Yes")), Some(json!(true)));
        assert_eq!(Vec::<i64>::lax(&json!([1, "2"])), Some(json!([1, 2])));
        assert_eq!(String::lax(&json!(1)), None);
    }
}

const CONFIDENCES: &[&str] = &["high", "medium", "low"];
crate::choice!(kvasir_types::results::Confidence, CONFIDENCES, |v: &Value| exact(CONFIDENCES, v));
