//! STORM's text clean-ups, as upstream's `ArticleTextProcessing` had them. Their quirks are kept:
//! port first, change behaviour after.

use std::sync::LazyLock;

use indexmap::IndexMap;
use regex::{Captures, Regex};

fn pattern(text: &str) -> Regex {
    Regex::new(text).expect("a valid pattern")
}

static CITATIONS: LazyLock<Regex> = LazyLock::new(|| pattern(r"\[\d+(?:,\s*\d+)*\]"));
static CITATION: LazyLock<Regex> = LazyLock::new(|| pattern(r"\[(\d+)\]"));
static GROUPED: LazyLock<Regex> = LazyLock::new(|| pattern(r"\[([0-9, ]+)\]"));
static ADJACENT: LazyLock<Regex> = LazyLock::new(|| pattern(r"(\[\d+\])+"));
static SENTENCE_END: LazyLock<Regex> = LazyLock::new(|| pattern(r"([.!?])\s*(\[\d+\])?\s*"));
static BRACKETED: LazyLock<Regex> = LazyLock::new(|| pattern(r"\[.*?\]"));
// The sections an outline should not have. Each runs to the next `##` or the end, as Python's `$`
// without MULTILINE matches before a final newline too. "Further reading*?" is upstream's: it
// matches only where the heading ends the outline.
static NOT_SECTIONS: LazyLock<Vec<fancy_regex::Regex>> = LazyLock::new(|| {
    [
        "See also.*?",
        "See Also.*?",
        "Notes.*?",
        "References.*?",
        "External links.*?",
        "External Links.*?",
        "Bibliography.*?",
        "Further reading*?",
        "Further Reading*?",
        "Summary.*?",
        "Appendices.*?",
        "Appendix.*?",
    ]
    .iter()
    .map(|heading| {
        fancy_regex::Regex::new(&format!(r"(?s)#[#]? {heading}(?=##|\n?\z)"))
            .expect("a valid pattern")
    })
    .collect()
});

/// The citation numbers in `text`, in order, as `[n]` gives them.
pub fn citation_numbers(text: &str) -> Vec<u64> {
    CITATION.captures_iter(text).filter_map(|found| found[1].parse().ok()).collect()
}

/// At most `max` words of `text`, cut at a word, with each line kept whole up to the cut. Empty
/// lines go.
pub fn limit_words(text: &str, max: usize) -> String {
    let mut count = 0;
    let mut limited = String::new();
    for line in text.split('\n') {
        for word in line.split_whitespace() {
            if count >= max {
                break;
            }
            limited.push_str(word);
            limited.push(' ');
            count += 1;
        }
        if count >= max {
            break;
        }
        limited = format!("{}\n", limited.trim());
    }
    limited.trim().to_owned()
}

/// `text` without its `[1]` or `[1, 2]` citations.
pub fn remove_citations(text: &str) -> String {
    CITATIONS.replace_all(text, "").into_owned()
}

/// `text` up to its last finished sentence and that sentence's citation, with `[1, 2]` split into
/// `[1] [2]` and each run of citations sorted without repeats.
pub fn finish_sentences(text: &str) -> String {
    let text = GROUPED.replace_all(text, |found: &Captures<'_>| {
        found[1].split(", ").map(|n| format!("[{n}]")).collect::<Vec<_>>().join(" ")
    });
    let text = ADJACENT.replace_all(&text, |found: &Captures<'_>| {
        let mut cited: Vec<(u64, &str)> = Vec::new();
        for single in CITATION.captures_iter(found.get(0).map_or("", |m| m.as_str())) {
            let whole = single.get(0).map_or("", |m| m.as_str());
            if !cited.iter().any(|(_, seen)| *seen == whole) {
                cited.push((single[1].parse().unwrap_or(u64::MAX), whole));
            }
        }
        cited.sort_by_key(|(number, _)| *number);
        cited.into_iter().map(|(_, whole)| whole).collect::<String>()
    });
    match SENTENCE_END.find_iter(&text).last() {
        Some(last) => text[..last.end()].trim().to_owned(),
        None => text.into_owned(),
    }
}

/// An expert's answer without a references list, an `Answer:` label, citations past the
/// `results` it was given, or an unfinished last sentence.
pub fn clean_answer(answer: &str, results: usize) -> String {
    let mut answer = answer.to_owned();
    for list in ["References:", "Sources:"] {
        if let Some(at) = answer.find(list) {
            answer.truncate(at);
        }
    }
    let mut answer = answer.replace("Answer:", "").trim().to_owned();
    answer = without_citations_from(&answer, results as u64);
    finish_sentences(&answer)
}

/// `text` without its citations numbered `from` or more, when any is past `from`. Upstream started
/// at the count of sources rather than past it, so the last valid citation goes too; kept.
pub fn without_citations_from(text: &str, from: u64) -> String {
    let numbers = citation_numbers(text);
    if numbers.iter().all(|&n| n <= from) {
        return text.to_owned();
    }
    let mut text = text.to_owned();
    for n in numbers.into_iter().filter(|&n| n >= from) {
        text = text.replace(&format!("[{n}]"), "");
    }
    text
}

/// An outline as headings only: a `-` item becomes a heading one level below the last, the
/// sections a page does not write are dropped, and so is anything in brackets.
pub fn clean_outline(outline: &str) -> String {
    let mut lines = Vec::new();
    let mut level = 0;
    for line in outline.split('\n') {
        let line = line.trim();
        if line.starts_with('#') {
            level = line.matches('#').count();
            lines.push(line.to_owned());
        } else if let Some(item) = line.strip_prefix('-') {
            lines.push(format!("{} {}", "#".repeat(level + 1), item.trim()));
        }
    }
    let mut outline = lines.join("\n");
    for section in NOT_SECTIONS.iter() {
        outline = section.replace_all(&outline, "").into_owned();
    }
    BRACKETED.replace_all(&outline, "").into_owned()
}

/// A written section with its unfinished sentences and its summary paragraphs dropped, and its
/// paragraphs a blank line apart.
pub fn clean_section(text: &str) -> String {
    let mut paragraphs = Vec::new();
    let mut in_summary = false;
    for paragraph in text.split('\n') {
        let paragraph = paragraph.trim();
        if paragraph.is_empty() {
            continue;
        }
        let paragraph = if paragraph.starts_with('#') {
            paragraph.to_owned()
        } else {
            finish_sentences(paragraph)
        };
        if in_summary {
            if paragraph.starts_with('#') {
                in_summary = false;
            } else {
                continue;
            }
        }
        if ["Overall", "In summary", "In conclusion"].iter().any(|s| paragraph.starts_with(s)) {
            continue;
        }
        if paragraph.contains("# Summary") || paragraph.contains("# Conclusion") {
            in_summary = true;
            continue;
        }
        paragraphs.push(paragraph);
    }
    paragraphs.join("\n\n")
}

/// `text` with each `[from]` of `map` made `[to]`, all at once.
pub fn renumber(text: &str, map: &[(u64, u64)]) -> String {
    let mut text = text.to_owned();
    for (from, _) in map {
        text = text.replace(&format!("[{from}]"), &format!("__PLACEHOLDER_{from}__"));
    }
    for (from, to) in map {
        text = text.replace(&format!("__PLACEHOLDER_{from}__"), &format!("[{to}]"));
    }
    text
}

/// One section of a parsed article: its text, and its subsections by title.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Parsed {
    pub content: String,
    pub subsections: IndexMap<String, Parsed>,
}

/// An article's sections by title, nested by their `#` headings. A heading's level is the count of
/// `#` anywhere in its line; a title used twice at one level keeps its place and the later text.
pub fn parse_article(text: &str) -> IndexMap<String, Parsed> {
    // Built as an arena first: a section is replaced in place when its title comes again.
    struct Section {
        content: String,
        children: IndexMap<String, usize>,
    }
    fn build(sections: &[Section], at: usize) -> Parsed {
        let section = &sections[at];
        Parsed {
            content: section.content.clone(),
            subsections: section
                .children
                .iter()
                .map(|(title, &child)| (title.clone(), build(sections, child)))
                .collect(),
        }
    }
    let mut sections = vec![Section { content: String::new(), children: IndexMap::new() }];
    let mut path: Vec<(usize, i64)> = vec![(0, -1)];
    for line in text.split('\n').filter(|line| !line.trim().is_empty()) {
        if line.starts_with('#') {
            let level = line.matches('#').count() as i64;
            let title = line.trim_matches(|c| c == '#' || c == ' ').trim().to_owned();
            while path.last().is_some_and(|&(_, at)| at >= level) {
                path.pop();
            }
            sections.push(Section { content: String::new(), children: IndexMap::new() });
            let new = sections.len() - 1;
            let parent = path.last().expect("the root is never popped").0;
            sections[parent].children.insert(title, new);
            path.push((new, level));
        } else {
            let at = path.last().expect("the root is never popped").0;
            sections[at].content.push_str(line);
            sections[at].content.push('\n');
        }
    }
    build(&sections, 0).subsections
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn words_are_limited_and_lines_kept_whole() {
        assert_eq!(limit_words("a b\n\nc d e\nf", 4), "a b\nc d");
        assert_eq!(limit_words("a b\n\nc", 10), "a b\nc");
    }

    #[test]
    fn citations_are_split_sorted_and_the_sentence_finished() {
        assert_eq!(remove_citations("A [1] b [2, 3]."), "A  b .");
        assert_eq!(finish_sentences("One [2][1][2]. Two [1, 3]."), "One [1][2]. Two [1] [3].");
        assert_eq!(finish_sentences("Done.[1] Not done"), "Done.[1]");
        assert_eq!(finish_sentences("no end"), "no end");
    }

    #[test]
    fn an_answer_loses_its_references_and_citations_past_its_sources() {
        assert_eq!(clean_answer("Answer: Yes [1][3]. References: [1] x", 2), "Yes [1].");
        assert_eq!(clean_answer("Yes [1][2].", 2), "Yes [1][2].");
    }

    #[test]
    fn an_outline_is_headings_without_what_a_page_does_not_write() {
        let outline = "Intro text\n# History\n- Early\n## See also\nlinks\n## Use [1]";
        assert_eq!(clean_outline(outline), "# History\n## Early\n## Use ");
        assert_eq!(clean_outline("# A\n## Further reading"), "# A\n");
        assert_eq!(clean_outline("# A\n## Further reading\n## B"), "# A\n## Further reading\n## B");
    }

    #[test]
    fn a_section_drops_its_summary() {
        let section = "# A\nText.\nIn summary, x.\n## Summary\ngone.\n## B\nkept.";
        assert_eq!(clean_section(section), "# A\n\nText.\n\n## B\n\nkept.");
    }

    #[test]
    fn citations_are_renumbered_at_once() {
        assert_eq!(renumber("[1][2]", &[(1, 2), (2, 1)]), "[2][1]");
    }

    #[test]
    fn an_article_is_parsed_by_its_headings() {
        let parsed = parse_article("# A\ntext\n## B\nmore\n# C\n# A\nagain");
        let titles: Vec<&String> = parsed.keys().collect();
        assert_eq!(titles, ["A", "C"]);
        assert_eq!(parsed["A"].content, "again\n");
        assert!(parsed["A"].subsections.is_empty());
    }
}
