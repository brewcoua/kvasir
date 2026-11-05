//! JSON values written as Python's `repr` writes them, so messages read as they did.

use serde_json::Value;

pub(crate) fn repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => match n.as_f64() {
            Some(f) if n.is_f64() => float(f),
            _ => n.to_string(),
        },
        Value::String(s) => text(s),
        Value::Array(items) => {
            format!("[{}]", items.iter().map(repr).collect::<Vec<_>>().join(", "))
        }
        Value::Object(map) => {
            let items: Vec<_> =
                map.iter().map(|(k, v)| format!("{}: {}", text(k), repr(v))).collect();
            format!("{{{}}}", items.join(", "))
        }
    }
}

/// Python's `repr(str)`: single quotes unless the text holds one and no double quote.
///
/// Only control characters and whitespace other than the space are escaped; Python also escapes
/// format characters such as U+200B, which this does not.
pub(crate) fn text(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if c.is_control() || (c.is_whitespace() && c != ' ') => match u32::from(c) {
                n @ ..0x100 => out.push_str(&format!("\\x{n:02x}")),
                n @ ..0x10000 => out.push_str(&format!("\\u{n:04x}")),
                n => out.push_str(&format!("\\U{n:08x}")),
            },
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python's `repr(float)`: the shortest digits that read back, positional from 1e-4 to below 1e16.
pub(crate) fn float(f: f64) -> String {
    let sci = format!("{f:e}");
    let (mantissa, exp) = sci.split_once('e').expect("{:e} writes an exponent");
    let exp: i32 = exp.parse().expect("{:e} writes an integer exponent");
    let (sign, mantissa) = mantissa.strip_prefix('-').map_or(("", mantissa), |m| ("-", m));
    let digits = mantissa.replace('.', "");
    if (-4..16).contains(&exp) {
        let point = exp + 1;
        if point <= 0 {
            format!("{sign}0.{}{digits}", "0".repeat(point.unsigned_abs() as usize))
        } else {
            let point = point as usize;
            let digits = format!("{digits:0<point$}");
            let (whole, frac) = digits.split_at(point);
            format!("{sign}{whole}.{}", if frac.is_empty() { "0" } else { frac })
        }
    } else {
        let (first, rest) = digits.split_at(1);
        let rest = if rest.is_empty() { String::new() } else { format!(".{rest}") };
        let esign = if exp < 0 { '-' } else { '+' };
        format!("{sign}{first}{rest}e{esign}{:02}", exp.unsigned_abs())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn floats() {
        for (f, want) in [
            (3.0, "3.0"),
            (0.25, "0.25"),
            (-0.5, "-0.5"),
            (-0.0, "-0.0"),
            (123.0, "123.0"),
            (1234.5, "1234.5"),
            (1e-7, "1e-07"),
            (0.0001, "0.0001"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1.5e300, "1.5e+300"),
            (0.1 + 0.2, "0.30000000000000004"),
        ] {
            assert_eq!(float(f), want);
        }
    }

    #[test]
    fn values() {
        assert_eq!(
            repr(&json!({"a": "it's", "b": [1, true, null, 2.0]})),
            r#"{'a': "it's", 'b': [1, True, None, 2.0]}"#
        );
        assert_eq!(repr(&json!("it's \"x\"\n")), r#"'it\'s "x"\n'"#);
        assert_eq!(repr(&json!("a\\b\u{1}\u{a0}é")), r"'a\\b\x01\xa0é'");
    }
}
