//! Prompts and replies in dspy 3.3's formats: `ChatAdapter`'s `[[ ## field ## ]]` sections, and
//! `JSONAdapter`'s object, which a call falls back to when a chat reply does not parse.
//!
//! Rendering is byte for byte what dspy sent, which `tests/golden/signatures.json` checks. Parsing
//! follows dspy's lenient reading: JSON is repaired as `json_repair` repairs it, and values are
//! coerced as pydantic's lax mode coerces them.

use kvasir_types::python_lines;
use serde_json::{Map, Value};

use crate::Message;
use crate::shape::{Kind, dump, schema_text};
use crate::signature::Field;

const INDENT: &str = "        ";
const INTRO: &str = "All interactions will be structured in the following way, with the appropriate values filled in.";

/// Which of dspy's two formats.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Chat,
    Json,
}

/// The system and user messages for one call.
pub fn render(
    format: Format,
    instructions: &str,
    inputs: &[Field],
    outputs: &[Field],
    values: &Map<String, Value>,
) -> Vec<Message> {
    let description = format!(
        "Your input fields are:\n{}\nYour output fields are:\n{}",
        describe(inputs),
        describe(outputs)
    );
    let structure = match format {
        Format::Chat => {
            [INTRO, &sections(inputs, true), &sections(outputs, false), "[[ ## completed ## ]]\n"]
                .join("\n\n")
        }
        Format::Json => {
            let shape: Map<String, Value> =
                outputs.iter().map(|f| (f.name.into(), translate(f, false).into())).collect();
            [
                INTRO,
                "Inputs will have the following structure:",
                &sections(inputs, true),
                "Outputs will be a JSON object with the following fields.",
                &serde_json::to_string_pretty(&shape).expect("strings serialize"),
            ]
            .join("\n\n")
        }
    };
    let system = format!("{description}\n{}\n{}", structure.trim(), objective(instructions));

    let type_note = |f: &Field| match f.kind {
        Kind::Str => String::new(),
        _ => format!(" (must be formatted as a valid Python {})", f.type_name),
    };
    let request = match format {
        Format::Chat => format!(
            "Respond with the corresponding output fields, starting with the field {}, and then \
             ending with the marker for `[[ ## completed ## ]]`.",
            outputs
                .iter()
                .map(|f| format!("`[[ ## {} ## ]]`{}", f.name, type_note(f)))
                .collect::<Vec<_>>()
                .join(", then ")
        ),
        Format::Json => format!(
            "Respond with a JSON object in the following order of fields: {}.",
            outputs
                .iter()
                .map(|f| format!("`{}`{}", f.name, type_note(f)))
                .collect::<Vec<_>>()
                .join(", then ")
        ),
    };
    let mut parts: Vec<String> = inputs
        .iter()
        .filter_map(|f| {
            Some(format!("[[ ## {} ## ]]\n{}", f.name, input_text(values.get(f.name)?)))
        })
        .collect();
    parts.push(request);
    vec![Message::system(system), Message::user(parts.join("\n\n").trim())]
}

/// An input value as dspy's `format_field_value` writes it: text as is, anything else as Python's
/// `str` or `json.dumps` would.
fn input_text(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => n.to_string(),
        other => {
            let mut out = String::new();
            dump(other, &mut out);
            out
        }
    }
}

fn describe(fields: &[Field]) -> String {
    let lines: Vec<String> = fields
        .iter()
        .enumerate()
        .map(|(i, f)| format!("{}. `{}` ({}): {}", i + 1, f.name, f.type_name, f.description))
        .collect();
    lines.join("\n").trim().to_owned()
}

fn translate(f: &Field, input: bool) -> String {
    let note = match f.kind {
        _ if input => String::new(),
        Kind::Str => String::new(),
        Kind::Bool => "must be True or False".into(),
        Kind::Int => "must be a single int value".into(),
        Kind::Float => "must be a single float value".into(),
        Kind::Literal(values) => {
            format!("must exactly match (no extra characters) one of: {}", values.join("; "))
        }
        Kind::Schema => format!("must adhere to the JSON schema: {}", schema_text(&f.schema)),
    };
    if note.is_empty() {
        format!("{{{}}}", f.name)
    } else {
        format!("{{{}}}{INDENT}# note: the value you produce {note}", f.name)
    }
}

fn sections(fields: &[Field], input: bool) -> String {
    let parts: Vec<String> =
        fields.iter().map(|f| format!("[[ ## {} ## ]]\n{}", f.name, translate(f, input))).collect();
    parts.join("\n\n").trim().to_owned()
}

/// `textwrap.dedent`, then each line indented by eight spaces, as dspy writes the task.
fn objective(instructions: &str) -> String {
    let margin = instructions
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| l.len() - l.trim_start().len())
        .min()
        .unwrap_or(0);
    let mut out = String::from("In adhering to this structure, your objective is: ");
    for line in instructions.lines() {
        out.push('\n');
        out.push_str(INDENT);
        if !line.trim().is_empty() {
            out.push_str(&line[margin..]);
        }
    }
    out
}

/// The output fields of a reply, or `None` when it does not parse as `format`.
pub fn parse(format: Format, outputs: &[Field], reply: &str) -> Option<Map<String, Value>> {
    match format {
        Format::Chat => parse_chat(outputs, reply),
        Format::Json => parse_json(outputs, reply),
    }
}

fn parse_chat(outputs: &[Field], reply: &str) -> Option<Map<String, Value>> {
    let mut sections: Vec<(Option<&str>, Vec<&str>)> = vec![(None, Vec::new())];
    for line in python_lines(reply) {
        let trimmed = line.trim();
        match header(trimmed) {
            Some((name, rest)) => {
                let rest = rest.trim();
                sections.push((Some(name), if rest.is_empty() { vec![] } else { vec![rest] }));
            }
            None => sections.last_mut().expect("starts with one").1.push(line),
        }
    }
    let mut got = Map::new();
    for (name, lines) in sections {
        let Some(name) = name else { continue };
        if got.contains_key(name) {
            continue;
        }
        let Some(field) = outputs.iter().find(|f| f.name == name) else { continue };
        let text = lines.join("\n").trim().to_owned();
        got.insert(name.into(), value(field, &Value::String(text))?);
    }
    (got.len() == outputs.len()).then_some(got)
}

/// `[[ ## name ## ]]` at the start of a line, with what follows it, as dspy's
/// `^\[\[ ## (\w+) ## \]\]` matches.
fn header(line: &str) -> Option<(&str, &str)> {
    let rest = line.strip_prefix("[[ ## ")?;
    let end = rest.find(|c: char| !(c.is_alphanumeric() || c == '_'))?;
    let (name, rest) = rest.split_at(end);
    let rest = rest.strip_prefix(" ## ]]")?;
    (!name.is_empty()).then_some((name, rest))
}

fn parse_json(outputs: &[Field], reply: &str) -> Option<Map<String, Value>> {
    let mut found = repair(reply)?;
    if !found.is_object() {
        let (start, end) = (reply.find('{')?, reply.rfind('}')?);
        found = repair(reply.get(start..=end)?)?;
    }
    let object = found.as_object()?;
    outputs.iter().map(|f| Some((f.name.into(), value(f, object.get(f.name)?)?))).collect()
}

fn repair(text: &str) -> Option<Value> {
    json_repair::loads_as::<Value>(text).ok()
}

/// A field's value from a reply: a chat section's text, or a value from a JSON reply.
fn value(field: &Field, raw: &Value) -> Option<Value> {
    match field.kind {
        Kind::Str => Some(match raw {
            Value::String(_) => raw.clone(),
            Value::Null => "None".into(),
            Value::Bool(b) => if *b { "True" } else { "False" }.into(),
            other => other.to_string().into(),
        }),
        Kind::Literal(allowed) => {
            if allowed.iter().any(|a| raw == *a) {
                return Some(raw.clone());
            }
            let mut text = raw.as_str()?.trim();
            if (text.starts_with("Literal[") || text.starts_with("str[")) && text.ends_with(']') {
                text = &text[text.find('[')? + 1..text.len() - 1];
            }
            let bytes = text.as_bytes();
            if bytes.len() > 1
                && bytes[0] == bytes[bytes.len() - 1]
                && (bytes[0] == b'"' || bytes[0] == b'\'')
            {
                text = &text[1..text.len() - 1];
            }
            allowed.iter().find(|a| **a == text).map(|a| (*a).into())
        }
        _ => {
            let candidate = match raw {
                Value::String(s) => match repair(s) {
                    // json_repair gives "" for text it cannot read, and dspy then tries the text.
                    Some(v) if v != "" || s.is_empty() => v,
                    _ => raw.clone(),
                },
                other => other.clone(),
            };
            (field.lax)(&candidate)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn headers() {
        assert_eq!(header("[[ ## claims ## ]]"), Some(("claims", "")));
        assert_eq!(header("[[ ## a_b ## ]] rest"), Some(("a_b", " rest")));
        assert_eq!(header("[[ ## a b ## ]]"), None);
        assert_eq!(header("[[ ##  ## ]]"), None);
    }

    #[test]
    fn lines() {
        assert_eq!(python_lines("a\r\nb\rc\n"), ["a", "b", "c"]);
        assert_eq!(python_lines("a\n\nb"), ["a", "", "b"]);
    }
}
