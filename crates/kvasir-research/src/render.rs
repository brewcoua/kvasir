//! The report as markdown, with claim ids turned into numbered sources.

use std::collections::HashMap;
use std::sync::LazyLock;

use kvasir_types::results::{Cell, Citation, Criterion, Row};
use regex::{Captures, Regex};

use crate::claims::Claim;

fn pattern(source: &str) -> Regex {
    Regex::new(source).expect("a valid pattern")
}

// `[3]`, and `[3, 7]`, which models write despite being asked for `[3][7]`.
pub(crate) static MARKER: LazyLock<Regex> = LazyLock::new(|| pattern(r"\[(\d+(?:\s*,\s*\d+)*)\]"));
static HEADING: LazyLock<Regex> = LazyLock::new(|| pattern(r"^(#{2,3}) +(.+?) *#*$"));
static SLUG_DROPPED: LazyLock<Regex> = LazyLock::new(|| pattern(r"[^\w\s-]"));
/// The summary's title, under which verification reports what it removed from it.
pub const SUMMARY: &str = "TL;DR";
/// Sections a report needs before contents help a reader find their way.
const CONTENTS_SECTIONS: usize = 3;

/// `[n]` repeated back to back, as one.
fn unrepeated(text: &str) -> String {
    let mut out = String::new();
    let mut last: Option<(usize, &str)> = None;
    for found in pattern(r"\[\d+\]").find_iter(text) {
        let repeat =
            last.is_some_and(|(end, marker)| end == found.start() && marker == found.as_str());
        let from = last.map_or(0, |(end, _)| end);
        if !repeat {
            out += &text[from..found.start()];
            out += found.as_str();
        }
        last = Some((found.end(), found.as_str()));
    }
    out + &text[last.map_or(0, |(end, _)| end)..]
}

/// The report, its sources numbered by first citation: one number per page, not per claim.
///
/// A marker naming a claim that does not exist is dropped: the model made it up. The `summary`
/// goes first, as a TL;DR callout, then the contents once there are enough sections, as callouts
/// GitHub and Obsidian render and other renderers show as quotes.
pub fn render(
    sections: &[(String, String)],
    claims: &HashMap<u64, Claim>,
    summary: &str,
) -> (String, Vec<Citation>) {
    let mut numbers: HashMap<String, u64> = HashMap::new();
    let mut citations = Vec::new();
    let mut cited = |text: &str| {
        let renumbered = MARKER.replace_all(text, |caps: &Captures| {
            let mut markers = String::new();
            for found in caps[1].split(',') {
                let Some(claim) = found.trim().parse().ok().and_then(|id: u64| claims.get(&id))
                else {
                    continue;
                };
                let next = numbers.len() as u64 + 1;
                let number = *numbers.entry(claim.url.clone()).or_insert_with(|| {
                    citations.push(Citation {
                        index: next,
                        url: claim.url.clone(),
                        title: claim.title.clone(),
                        snippet: claim.quote.clone(),
                        source: None,
                    });
                    next
                });
                markers += &format!("[{number}]");
            }
            markers
        });
        unrepeated(&renumbered)
    };
    let mut parts = Vec::new();
    if !summary.trim().is_empty() {
        parts.push(callout("SUMMARY", SUMMARY, &cited(summary.trim())));
    }
    let body: Vec<String> = sections
        .iter()
        .map(|(title, text)| format!("## {title}\n\n{}", untitled(title, text.trim())))
        .collect();
    if sections.len() >= CONTENTS_SECTIONS {
        parts.push(callout("ABSTRACT", "Contents", &contents(&body)));
    }
    parts.extend(body.iter().map(|text| cited(text)));
    (parts.join("\n\n"), citations)
}

fn callout(kind: &str, title: &str, text: &str) -> String {
    let lines = text.split('\n').map(|line| format!("> {line}").trim_end().to_owned());
    [format!("> [!{kind}] {title}")].into_iter().chain(lines).collect::<Vec<_>>().join("\n")
}

/// A nested list linking to each `##` and `###` heading, by GitHub's anchors.
pub fn contents(sections: &[String]) -> String {
    let mut taken = HashMap::new();
    let mut lines = Vec::new();
    let mut fenced = false;
    for line in sections.join("\n\n").split('\n') {
        if line.starts_with("```") {
            fenced = !fenced;
        }
        let Some(found) = HEADING.captures(line).filter(|_| !fenced) else {
            continue;
        };
        let title =
            MARKER.replace_all(&found[2], "").split_whitespace().collect::<Vec<_>>().join(" ");
        let indent = "  ".repeat(found[1].len() - 2);
        lines.push(format!("{indent}- [{title}](#{})", slug(&title, &mut taken)));
    }
    lines.join("\n")
}

/// The anchor GitHub gives a heading, `-1`, `-2` and on added to one already taken.
pub fn slug(title: &str, taken: &mut HashMap<String, usize>) -> String {
    let base = SLUG_DROPPED.replace_all(title.to_lowercase().trim(), "").replace(' ', "-");
    let count = taken.entry(base.clone()).or_insert(0);
    *count += 1;
    if *count > 1 { format!("{base}-{}", *count - 1) } else { base }
}

/// The section without a first heading repeating its title, which models add anyway.
fn untitled(title: &str, text: &str) -> String {
    let (first, rest) = text.split_once('\n').unwrap_or((text, ""));
    if first.starts_with('#')
        && first.trim_start_matches('#').trim().to_lowercase() == title.trim().to_lowercase()
    {
        return rest.trim().to_owned();
    }
    text.to_owned()
}

/// The comparison as a markdown table, its cells cited by claim id like the prose.
pub fn matrix(rows: &[Row], criteria: &[Criterion]) -> String {
    let ranked = rows.first().is_some_and(|row| row.rank.is_some());
    let mut header: Vec<String> = if ranked { vec!["Rank".into()] } else { Vec::new() };
    header.push("Option".into());
    header.extend(criteria.iter().map(|c| plain(&c.name)));
    let mut lines =
        vec![format!("| {} |", header.join(" | ")), format!("|{}", " --- |".repeat(header.len()))];
    for row in rows {
        let mut cells: Vec<String> = Vec::new();
        if ranked {
            cells.push(row.rank.map_or_else(|| "None".to_owned(), |rank| rank.to_string()));
        }
        cells.push(plain(&row.entity));
        cells.extend(row.cells.iter().map(cell));
        lines.push(format!("| {} |", cells.join(" | ")));
    }
    let mut note = "A dash means no claim spoke to it.".to_owned();
    if ranked {
        let weights: Vec<String> =
            criteria.iter().map(|c| format!("{} {}", c.name, c.weight)).collect();
        note = format!(
            "Each criterion was ranked twice, the second time with the options in reverse order. \
             The overall rank is the mean of those places, weighted from 1 to 3 by importance: \
             {}. {note}",
            weights.join(", ")
        );
    }
    lines.push(String::new());
    lines.push(note);
    lines.join("\n")
}

fn cell(cell: &Cell) -> String {
    if cell.claims.is_empty() {
        return "–".into();
    }
    let markers: String = cell.claims.iter().map(|i| format!("[{i}]")).collect();
    format!("{} {markers}", plain(&cell.finding))
}

/// `text` safe inside a table cell.
fn plain(text: &str) -> String {
    text.replace('|', "/").split_whitespace().collect::<Vec<_>>().join(" ")
}
