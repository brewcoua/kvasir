//! A page cut into pieces small enough that one model call sees one piece, the notes and the
//! question, and nothing else.

use std::collections::BTreeMap;

use kvasir_types::python_lines;

// Joins paragraphs, and sections, in a piece: the blank line extraction puts between blocks.
const SEPARATOR: &str = "\n\n";
// A longer "heading" is a paragraph set in a heading tag. Repeated at the top of pieces it would
// crowd out their text.
const MAX_HEADING: usize = 200;

type Path = Vec<(usize, String)>;

fn size_of(text: &str) -> usize {
    text.chars().count()
}

/// Pieces of at most `size` characters, cut at the page's headings where it has any.
///
/// Sections are merged while they fit, never across a top-level heading: the shallowest level that
/// occurs more than once, so a page's lone title does not count. A section over `size` is cut
/// between paragraphs. Each piece begins with the headings above it that it does not start with,
/// as the page wrote them, so a quote from it stays on the page.
pub fn split(text: &str, size: usize) -> Vec<String> {
    let sections = sections(text);
    if sections.iter().all(|(path, _)| path.is_empty()) {
        return lines(text, size);
    }
    let mut levels = BTreeMap::new();
    for (path, _) in &sections {
        if let Some((level, _)) = path.last() {
            *levels.entry(*level).or_insert(0) += 1;
        }
    }
    let top = levels.iter().find(|(_, count)| **count > 1).map_or(0, |(level, _)| *level);
    let mut pieces = Vec::new();
    let mut block: Vec<(Path, String)> = Vec::new();
    for section in sections {
        if !block.is_empty() && section.0.last().is_some_and(|(level, _)| *level == top) {
            pieces.extend(merge(&block, size, top));
            block.clear();
        }
        block.push(section);
    }
    pieces.extend(merge(&block, size, top));
    pieces
}

/// A markdown heading line's level.
fn heading_level(line: &str) -> Option<usize> {
    let level = line.bytes().take_while(|b| *b == b'#').count();
    let rest = line[level..].strip_prefix(' ')?;
    ((1..=6).contains(&level) && rest.starts_with(|c: char| !c.is_whitespace())).then_some(level)
}

/// The text cut before each heading line, with the headings above each section and its own.
fn sections(text: &str) -> Vec<(Path, String)> {
    let mut sections: Vec<(Path, Vec<&str>)> = vec![(Vec::new(), Vec::new())];
    let mut path: Path = Vec::new();
    let mut fenced = false;
    for line in python_lines(text) {
        // A "# comment" in a code block is not a heading.
        fenced ^= line.starts_with("```");
        let level =
            (!fenced && size_of(line) <= MAX_HEADING).then(|| heading_level(line)).flatten();
        if let Some(level) = level {
            path.retain(|(above, _)| *above < level);
            path.push((level, line.to_owned()));
            sections.push((path.clone(), Vec::new()));
        }
        sections.last_mut().expect("never empty").1.push(line);
    }
    sections
        .into_iter()
        .filter(|(_, lines)| lines.iter().any(|line| !line.trim().is_empty()))
        .map(|(path, lines)| (path, lines.join("\n").trim_matches('\n').to_owned()))
        .collect()
}

fn heading_lines(path: &[(usize, String)], top: usize) -> String {
    // Above the top level is the page's title, which the reader is already given.
    path.iter().filter(|(level, _)| *level >= top).map(|(_, line)| format!("{line}\n")).collect()
}

fn merge(sections: &[(Path, String)], size: usize, top: usize) -> Vec<String> {
    let mut pieces = Vec::new();
    let mut current = String::new();
    for (path, body) in sections {
        let ancestors = heading_lines(&path[..path.len().saturating_sub(1)], top);
        let whole = heading_lines(path, top);
        let parts: Vec<(&str, String)> = if size_of(&ancestors) + size_of(body) <= size {
            vec![(&ancestors, body.clone())]
        } else {
            let room = size.saturating_sub(size_of(&whole)).max(1);
            let cut = join(paragraphs(body, room), room);
            cut.into_iter()
                .enumerate()
                .map(|(i, part)| (if i == 0 { &ancestors } else { &whole }.as_str(), part))
                .collect()
        };
        for (lead, part) in parts {
            if !current.is_empty()
                && size_of(&current) + size_of(SEPARATOR) + size_of(&part) <= size
            {
                current.push_str(SEPARATOR);
                current.push_str(&part);
            } else {
                if !current.is_empty() {
                    pieces.push(std::mem::take(&mut current));
                }
                current = format!("{lead}{part}");
            }
        }
    }
    if !current.is_empty() {
        pieces.push(current);
    }
    pieces
}

fn paragraphs(text: &str, size: usize) -> Vec<String> {
    text.split(SEPARATOR).flat_map(|paragraph| lines(paragraph, size)).collect()
}

fn join(parts: Vec<String>, size: usize) -> Vec<String> {
    let mut joined: Vec<String> = Vec::new();
    for part in parts {
        match joined.last_mut() {
            Some(last) if size_of(last) + size_of(SEPARATOR) + size_of(&part) <= size => {
                last.push_str(SEPARATOR);
                last.push_str(&part);
            }
            _ => joined.push(part),
        }
    }
    joined
}

/// Pieces of at most `size` characters, cut between lines where a line allows it.
fn lines(text: &str, size: usize) -> Vec<String> {
    let mut pieces = Vec::new();
    let mut current = String::new();
    for line in python_lines(text) {
        let mut line = line;
        while size_of(line) > size {
            if !current.is_empty() {
                pieces.push(std::mem::take(&mut current));
            }
            let cut = crate::head(line, size);
            pieces.push(cut.to_owned());
            line = &line[cut.len()..];
        }
        if !current.is_empty() && size_of(&current) + 1 + size_of(line) > size {
            pieces.push(std::mem::take(&mut current));
        }
        if current.is_empty() {
            current = line.to_owned();
        } else {
            current.push('\n');
            current.push_str(line);
        }
    }
    if !current.is_empty() {
        pieces.push(current);
    }
    pieces
}
