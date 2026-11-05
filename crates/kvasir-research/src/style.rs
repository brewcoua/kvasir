//! Taking out of written prose the tics a model adds whatever it is told.
//!
//! The writer is asked not to use them; this catches what it uses anyway. Only words that carry no
//! fact are removed, and nothing inside double quotes or code, so verification still sees every
//! claim.

use std::sync::LazyLock;

use fancy_regex::Regex;

fn pattern(source: &str) -> Regex {
    Regex::new(source).expect("a valid pattern")
}

// Openers that announce a point instead of making it. Taken off the start of a sentence, which
// then starts at the next word.
static OPENERS: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"(?<![^\s>*-])(?:It is (?:important|crucial) to note that\s+|",
        r"It(?:'s| is) worth (?:noting|mentioning) that\s+|(?:Notably|Importantly|Interestingly|",
        r"Ultimately|Essentially|Fundamentally|Crucially|In conclusion|Overall|In essence|",
        r"Indeed|Moreover|Furthermore|Additionally),\s+)([\w*])",
    ))
});
// Intensifiers, wherever they are.
static FILLER: LazyLock<Regex> = LazyLock::new(|| {
    pattern(r"(?i)\s+\b(?:really|truly|very|actually|basically|simply|quite)\b(?=\s)")
});
// An em dash between numbers is a range; anywhere else, an aside a comma sets off as well.
static RANGE: LazyLock<Regex> = LazyLock::new(|| pattern(r"(?<=\d)\s*—\s*(?=\d)"));
static DASH: LazyLock<Regex> = LazyLock::new(|| pattern(r"\s*—\s*"));
// What is left as written: code, and anything in double quotes.
static KEPT: LazyLock<Regex> = LazyLock::new(|| pattern(r#"(`[^`\n]*`|"[^"\n]*"|“[^”\n]*”)"#));

/// `text` without filler openers, intensifiers and em dashes, outside quotes and code.
pub fn clean(text: &str) -> String {
    let mut fenced = false;
    let lines: Vec<String> = text
        .split('\n')
        .map(|line| {
            let fence = line.trim_start().starts_with("```");
            if fence {
                fenced = !fenced;
            }
            if fenced || fence { line.to_owned() } else { prose_line(line) }
        })
        .collect();
    let cleaned = lines.join("\n");
    if cleaned != text {
        tracing::info!(
            "took filler out of the prose: {} characters",
            text.chars().count() as i64 - cleaned.chars().count() as i64
        );
    }
    cleaned
}

/// The prose of `line` cleaned, its quotes and code kept as they are.
fn prose_line(line: &str) -> String {
    let mut out = String::new();
    let mut at = 0;
    for kept in KEPT.find_iter(line).filter_map(Result::ok) {
        out += &prose(&line[at..kept.start()]);
        out += kept.as_str();
        at = kept.end();
    }
    out + &prose(&line[at..])
}

fn prose(text: &str) -> String {
    let text =
        OPENERS.replace_all(text, |caps: &fancy_regex::Captures<'_, str>| caps[1].to_uppercase());
    let text = FILLER.replace_all(&text, "");
    let text = RANGE.replace_all(&text, "–");
    DASH.replace_all(&text, ", ").into_owned()
}
