//! Types and small pure functions shared by every kvasir crate.

pub mod sites;

/// `text` cut as Python's `str.splitlines` cuts it, which more than a newline breaks.
pub fn python_lines(text: &str) -> Vec<&str> {
    const BREAKS: [char; 10] = [
        '\n', '\r', '\u{0b}', '\u{0c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
        '\u{2029}',
    ];
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if BREAKS.contains(&c) {
            lines.push(&text[start..i]);
            start = i + c.len_utf8();
            if c == '\r' && chars.next_if(|&(_, next)| next == '\n').is_some() {
                start += 1;
            }
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}
