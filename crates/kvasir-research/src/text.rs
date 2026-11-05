//! Matching text word for word, as quotes and names are matched.

use std::sync::LazyLock;

use kvasir_types::python_lines;
use regex::Regex;

/// Words that reverse or condition what follows. A quote that starts right after one, or that skips
/// one with an ellipsis, can say the opposite of the page.
const QUALIFIERS: [&str; 9] =
    ["not", "no", "never", "unless", "if", "except", "without", "only", "cannot"];

fn pattern(source: &str) -> Regex {
    Regex::new(source).expect("a valid pattern")
}

static WORD: LazyLock<Regex> = LazyLock::new(|| pattern(r"\w+"));
static ELLIPSIS: LazyLock<Regex> = LazyLock::new(|| pattern(r"\.\.\.|…"));
static NOT: LazyLock<Regex> = LazyLock::new(|| pattern(r"n['’]t\b"));
static SENTENCE_END: LazyLock<Regex> = LazyLock::new(|| pattern(r"[.!?;]\s"));
// The line under a markdown table's header: "|---|:--:|".
static SEPARATOR: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$"));

/// Whether `quote` is in `text` word for word, an ellipsis standing for words left out.
///
/// Words only: models write a non-breaking hyphen where the page has a hyphen, join list items
/// without their bullets, and the extracted text has a break wherever the page had an inline tag,
/// as in "attention , a". None of that changes what the quote says.
pub fn quoted(quote: &str, text: &str) -> bool {
    placed(quote, text).is_some()
}

/// Whether a quote found in `text` can say what the page does not: the words just before it, or
/// the ones an ellipsis stands for, include a negator or qualifier.
///
/// "grows" quoted from "it never grows" is on the page, and means the opposite. The words before it
/// are taken from its own sentence only: "It is not. Cats grow" does not turn "Cats grow" around.
pub fn misleading(quote: &str, text: &str) -> bool {
    // "isn't" as "isn not", since `words` cuts it into "isn" and "t", like the "T" of "T cells".
    let text = NOT.replace_all(text, "n not");
    let Some(found) = placed(quote, &text) else {
        return false;
    };
    let haystack = words(&text);
    let skipped = found.windows(2).map(|pair| haystack[pair[0].1..=pair[1].0].to_owned());
    // The page's text up to the quote's first word, which `words` would have stripped of the full
    // stop that ends the sentence before it.
    let lowered = text.to_lowercase();
    let first = haystack[..found[0].0].split_whitespace().count();
    let start = WORD.find_iter(&lowered).nth(first).map_or(lowered.len(), |m| m.start());
    let up_to = &lowered[..start];
    let sentence = SENTENCE_END.find_iter(up_to).last().map_or(up_to, |m| &up_to[m.end()..]);
    let before: Vec<&str> = WORD.find_iter(sentence).map(|m| m.as_str()).collect();
    let before = before[before.len().saturating_sub(2)..].join(" ");
    [before]
        .into_iter()
        .chain(skipped)
        .any(|part| part.split_whitespace().any(|word| QUALIFIERS.contains(&word)))
}

/// Where each fragment of `quote` is in `words(text)`, as start and end, or None when a fragment
/// is not there.
pub fn placed(quote: &str, text: &str) -> Option<Vec<(usize, usize)>> {
    let haystack = words(text);
    // A fragment with no words is two spaces, which no page's words hold: a quote that is empty,
    // or ends in an ellipsis, is not on the page.
    let mut spans = Vec::new();
    let mut position = 0;
    for fragment in ELLIPSIS.split(quote).map(words) {
        let found = position + haystack[position..].find(&fragment)?;
        spans.push((found, found + fragment.len()));
        // Back one, so the next fragment can start at the space this one ended on.
        position = found + fragment.len() - 1;
    }
    Some(spans)
}

/// `quote` with the header row of the markdown table it is a row of put before it, after an
/// ellipsis, so "Cold start 1.5ms 3ms 2ms" says which runtime each figure is for. Still on the
/// page word for word. A quote outside a table, or one that has the header, is returned as is.
pub fn with_table_header(quote: &str, text: &str) -> String {
    let mut block: Vec<&str> = Vec::new();
    for line in python_lines(text).into_iter().chain([""]) {
        if line.trim_start().starts_with('|') {
            block.push(line.trim());
            continue;
        }
        if block.len() > 2 && SEPARATOR.is_match(block[1]) {
            let (header, rows) = (block[0], block[2..].join("\n"));
            if quoted(quote, &rows) && !quoted(quote, header) {
                return format!("{header} ... {quote}");
            }
        }
        block.clear();
    }
    quote.to_owned()
}

/// Whether `text` names `entity`: most of the words of its name appear in it.
///
/// Most rather than all, since sources shorten names: "Qwen3-Coder-30B" for "Qwen3-Coder-30B-A3B".
pub fn names(entity: &str, text: &str) -> bool {
    let name = words(entity);
    let parts: Vec<&str> = name.split_whitespace().collect();
    let text = words(text);
    let found = parts.iter().filter(|word| text.contains(&format!(" {word} "))).count();
    !parts.is_empty() && found * 2 > parts.len()
}

/// The words of `text`, lowercase, one space apart, and padded with a space each side, so a
/// fragment matches whole words only: "4 bit" is not found in "14 bits".
pub fn words(text: &str) -> String {
    let lowered = text.to_lowercase();
    let found: Vec<&str> = WORD.find_iter(&lowered).map(|m| m.as_str()).collect();
    format!(" {} ", found.join(" "))
}
