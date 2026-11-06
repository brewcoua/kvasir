//! An article as STORM builds it: a tree of sections under the topic, and the sources its `[n]`
//! citations number.
//!
//! The tree is an arena, as upstream's nodes were shared objects: a node a later step cuts from the
//! tree stays where it is, and whatever still holds it carries on without effect.

use std::collections::BTreeSet;

use indexmap::IndexMap;

use crate::text::{Parsed, citation_numbers, parse_article, renumber, without_citations_from};

/// A search result as STORM keeps it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Info {
    pub url: String,
    pub title: String,
    pub description: String,
    pub snippets: Vec<String>,
}

/// Adds `more` to `snippets`, each once.
pub fn add_snippets(snippets: &mut Vec<String>, more: &[String]) {
    for snippet in more {
        if !snippets.contains(snippet) {
            snippets.push(snippet.clone());
        }
    }
}

#[derive(Clone, Debug)]
struct Node {
    name: String,
    content: Option<String>,
    children: Vec<usize>,
}

const ROOT: usize = 0;

#[derive(Clone, Debug)]
pub struct Article {
    nodes: Vec<Node>,
    /// Each source's citation number.
    pub numbers: IndexMap<String, u64>,
    pub sources: IndexMap<String, Info>,
}

impl Article {
    pub fn new(topic: &str) -> Self {
        let root = Node { name: topic.to_owned(), content: None, children: Vec::new() };
        Self { nodes: vec![root], numbers: IndexMap::new(), sources: IndexMap::new() }
    }

    /// The article an outline of `#` headings gives, without text. A first heading that is the
    /// topic is dropped, and the rest moved up a level.
    pub fn from_outline(topic: &str, outline: &str) -> Self {
        let mut article = Self::new(topic);
        let mut lines: Vec<&str> =
            outline.split('\n').map(str::trim).filter(|line| !line.is_empty()).collect();
        let Some(first) = lines.first() else {
            return article;
        };
        let titled = first.starts_with('#')
            && first.replace('#', "").trim().to_lowercase()
                == topic.to_lowercase().replace('_', " ");
        if titled {
            lines.remove(0);
        }
        let mut path: Vec<(i64, usize)> = vec![(0, ROOT)];
        for line in lines {
            let level = line.matches('#').count() as i64 - i64::from(titled);
            let name = line.replace('#', "").trim().to_owned();
            if name == topic {
                continue;
            }
            // Upstream popped the topic too for a heading at its level, then failed on the empty
            // stack; such a heading goes under the topic instead.
            while path.len() > 1 && path.last().is_some_and(|&(at, _)| level <= at) {
                path.pop();
            }
            let parent = path.last().expect("the topic stays").1;
            let node = article.add(parent, &name, None, false);
            path.push((level, node));
        }
        article
    }

    fn add(&mut self, parent: usize, name: &str, content: Option<String>, front: bool) -> usize {
        self.nodes.push(Node { name: name.to_owned(), content, children: Vec::new() });
        let node = self.nodes.len() - 1;
        let children = &mut self.nodes[parent].children;
        if front {
            children.insert(0, node)
        } else {
            children.push(node)
        }
        node
    }

    /// The first section named `name` at or below `from`, depth first.
    fn find(&self, from: usize, name: &str) -> Option<usize> {
        if self.nodes[from].name == name {
            return Some(from);
        }
        self.nodes[from].children.iter().find_map(|&child| self.find(child, name))
    }

    /// The titles of the top-level sections.
    pub fn sections(&self) -> Vec<String> {
        self.nodes[ROOT].children.iter().map(|&child| self.nodes[child].name.clone()).collect()
    }

    /// The titles of `under` and every section below it, in order, as `#` headings or bare; or of
    /// every section but the topic when `under` is None.
    pub fn outline(&self, under: Option<&str>, headings: bool) -> Vec<String> {
        fn walk(
            article: &Article,
            node: usize,
            level: usize,
            headings: bool,
            out: &mut Vec<String>,
        ) {
            let name = &article.nodes[node].name;
            out.push(if headings {
                format!("{} {name}", "#".repeat(level)).trim().to_owned()
            } else {
                name.clone()
            });
            for &child in &article.nodes[node].children {
                walk(article, child, level + 1, headings, out);
            }
        }
        let mut out = Vec::new();
        match under {
            Some(name) => {
                if let Some(node) = self.find(ROOT, name) {
                    walk(self, node, 1, headings, &mut out);
                }
            }
            None => {
                for &child in &self.nodes[ROOT].children {
                    walk(self, child, 1, headings, &mut out);
                }
            }
        }
        out
    }

    /// Adds a written section, its citations, numbered against `sources`, renumbered against the
    /// article's. A citation past the sources is dropped.
    pub fn add_section(&mut self, written: &str, sources: &[Info]) {
        let written = without_citations_from(written, sources.len() as u64);
        let cited: BTreeSet<u64> = citation_numbers(&written).into_iter().collect();
        let mut map = Vec::new();
        for (i, source) in sources.iter().enumerate() {
            if !cited.contains(&(i as u64 + 1)) {
                continue;
            }
            let number = match self.numbers.get(&source.url) {
                Some(&number) => {
                    let known =
                        self.sources.get_mut(&source.url).expect("numbered sources are kept");
                    add_snippets(&mut known.snippets, &source.snippets);
                    number
                }
                None => {
                    let number = self.numbers.len() as u64 + 1;
                    self.numbers.insert(source.url.clone(), number);
                    self.sources.insert(source.url.clone(), source.clone());
                    number
                }
            };
            map.push((i as u64 + 1, number));
        }
        let parsed = parse_article(&renumber(&written, &map));
        self.insert(&parsed, None, false);
    }

    /// Puts each of `parsed` under `parent`, the topic when None: into the section of its title
    /// where there is one, else a new one, a `summary` at the front of the article. A section's
    /// subsections replace its own.
    pub fn insert(&mut self, parsed: &IndexMap<String, Parsed>, parent: Option<&str>, trim: bool) {
        let parent = match parent {
            None => ROOT,
            Some(name) => match self.find(ROOT, name) {
                Some(node) => node,
                None => return,
            },
        };
        if trim {
            let nodes = &self.nodes;
            let kept: Vec<usize> = nodes[parent]
                .children
                .iter()
                .copied()
                .filter(|&child| parsed.contains_key(&nodes[child].name))
                .collect();
            self.nodes[parent].children = kept;
        }
        for (name, section) in parsed {
            let content = Some(section.content.trim().to_owned());
            match self.find(parent, name) {
                Some(node) => self.nodes[node].content = content,
                None => {
                    let front =
                        self.nodes[parent].name == self.nodes[ROOT].name && name == "summary";
                    self.add(parent, name, content, front);
                }
            }
            self.insert(&section.subsections, Some(name), true);
        }
    }

    /// The article as `#` headings and text, a blank line apart.
    pub fn text(&self) -> String {
        fn walk(article: &Article, node: usize, level: usize, out: &mut Vec<String>) {
            let node = &article.nodes[node];
            out.push(format!("{} {}", "#".repeat(level), node.name).trim().to_owned());
            out.push(node.content.clone().unwrap_or_default());
            for &child in &node.children {
                walk(article, child, level + 1, out);
            }
        }
        let mut out = Vec::new();
        for &child in &self.nodes[ROOT].children {
            walk(self, child, 1, &mut out);
        }
        out.iter()
            .map(|part| part.trim())
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>()
            .join("\n\n")
    }

    /// Drops the sections with neither text nor subsections, then numbers the citations in the
    /// order they first appear, dropping the sources none cites.
    pub fn finish(&mut self) {
        self.prune(ROOT);
        let mut order = Vec::new();
        self.walk_content(ROOT, &mut |content| order.extend(citation_numbers(content)));
        let mut map: Vec<(u64, u64)> = Vec::new();
        for number in order {
            if !map.iter().any(|&(from, _)| from == number) {
                map.push((number, map.len() as u64 + 1));
            }
        }
        let mut contents = Vec::new();
        self.walk_nodes(ROOT, &mut contents);
        for node in contents {
            if let Some(content) = self.nodes[node].content.as_mut().filter(|c| !c.is_empty()) {
                *content = renumber(content, &map);
            }
        }
        self.numbers.retain(|_, number| match map.iter().find(|&&(from, _)| from == *number) {
            Some(&(_, to)) => {
                *number = to;
                true
            }
            None => false,
        });
    }

    /// Whether `node` is kept: it has text, or a subsection that is.
    fn prune(&mut self, node: usize) -> bool {
        let children = self.nodes[node].children.clone();
        let kept: Vec<usize> = children.into_iter().filter(|&child| self.prune(child)).collect();
        self.nodes[node].children = kept;
        let node = &self.nodes[node];
        node.content.as_ref().is_some_and(|c| !c.is_empty()) || !node.children.is_empty()
    }

    fn walk_nodes(&self, node: usize, out: &mut Vec<usize>) {
        out.push(node);
        for &child in &self.nodes[node].children {
            self.walk_nodes(child, out);
        }
    }

    fn walk_content(&self, node: usize, visit: &mut impl FnMut(&str)) {
        if let Some(content) = self.nodes[node].content.as_deref().filter(|c| !c.is_empty()) {
            visit(content);
        }
        for &child in &self.nodes[node].children {
            self.walk_content(child, visit);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source(url: &str) -> Info {
        Info {
            url: url.into(),
            title: url.into(),
            snippets: vec![format!("about {url}")],
            ..Info::default()
        }
    }

    #[test]
    fn an_outline_drops_a_first_heading_that_is_the_topic() {
        let article =
            Article::from_outline("Rok stone", "# Rok stone\n## History\n### Finding\n## Runes");
        assert_eq!(article.sections(), ["History", "Runes"]);
        assert_eq!(article.outline(None, true), ["# History", "## Finding", "# Runes"]);
        assert_eq!(article.outline(Some("History"), false), ["History", "Finding"]);
        let flat = Article::from_outline("Rok stone", "# Rok stone\n# History");
        assert_eq!(flat.sections(), ["History"]);
    }

    #[test]
    fn citations_are_numbered_by_first_appearance_across_sections() {
        let mut article = Article::from_outline("t", "# A\n# B");
        article.add_section("# B\nB cites [2] and [1].", &[source("x"), source("y")]);
        article.add_section("# A\nA cites [1].", &[source("y")]);
        article.finish();

        assert_eq!(article.text(), "# A\n\nA cites [1].\n\n# B\n\nB cites [1] and [2].");
        let numbers: Vec<(&str, u64)> =
            article.numbers.iter().map(|(url, &n)| (url.as_str(), n)).collect();
        assert_eq!(numbers, [("x", 2), ("y", 1)]);
    }

    #[test]
    fn a_written_section_replaces_its_outline_and_an_empty_one_goes() {
        let mut article = Article::from_outline("t", "# A\n## Sub\n# B");
        article.add_section("# A\nText.", &[]);
        article.finish();
        assert_eq!(article.text(), "# A\n\nText.");
    }

    #[test]
    fn a_summary_goes_first() {
        let mut article = Article::from_outline("t", "# A");
        article.add_section("# A\nText.", &[]);
        let polished = parse_article(&format!("# summary\nLead.\n\n{}", article.text()));
        article.insert(&polished, None, false);
        article.finish();
        assert_eq!(article.text(), "# summary\n\nLead.\n\n# A\n\nText.");
    }
}
