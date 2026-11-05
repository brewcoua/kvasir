//! HTML to markdown, and the page's links with where on the page each sits.

use std::collections::HashMap;
use std::sync::LazyLock;

use dom_query::{Document, NodeRef};
use regex::Regex;
use serde::{Deserialize, Serialize};
use url::Url;

use crate::normalize;

// Extraction returning less than this has missed the article, as on a page of short blocks, and
// the cleaned page's text is used instead. Untuned.
const MIN_TEXT: usize = 200;
// Characters of the block around a link that are kept, centred on its anchor.
const CONTEXT_CHARACTERS: usize = 200;

const BOILERPLATE: &str = "script, style, nav, header, footer, aside, form, noscript";
// Interactive elements, from web-som's SELECTORS, are controls and never prose. Links are kept
// because their text is read, and [tabindex] and [contenteditable] also mark content. #siteSub is
// Wikipedia's "From Wikipedia, the free encyclopedia", which rs-trafilatura keeps.
const INTERACTIVE: &str = concat!(
    r#"button, input:not([type="hidden"]), select, textarea, .btn, [role="button"], "#,
    r#"[role="checkbox"], [role="radio"], [role="input"], [role="menuitem"], "#,
    r#"[role="menuitemcheckbox"], [role="menuitemradio"], [role="option"], [role="switch"], "#,
    r#"[role="tab"], [role="treeitem"], [role="gridcell"], [role="search"], [role="combobox"], "#,
    r#"[role="listbox"], [role="slider"], [role="spinbutton"], #siteSub"#,
);
// Furniture rs-trafilatura leaves in its markdown, though not in its plain text: lists of links to
// neighbouring articles that can outweigh the article five times over.
const LINK_BOXES: &str = concat!(
    r#".navbox, .vertical-navbox, .catlinks, #catlinks, .printfooter, .sistersitebox, "#,
    r#"[role="navigation"], [role="contentinfo"]"#,
);
// Firecrawl's excludeNonMainTags, used when extraction finds too little. Not applied before it:
// extraction already drops the cookie banner and share box of the saved pages.
const NON_MAIN: [&str; 40] = [
    "header",
    "footer",
    "nav",
    "aside",
    ".header",
    ".top",
    ".navbar",
    "#header",
    ".footer",
    ".bottom",
    "#footer",
    ".sidebar",
    ".side",
    ".aside",
    "#sidebar",
    ".modal",
    ".popup",
    "#modal",
    ".overlay",
    ".ad",
    ".ads",
    ".advert",
    "#ad",
    ".lang-selector",
    ".language",
    "#language-selector",
    ".social",
    ".social-media",
    ".social-links",
    "#social",
    ".menu",
    ".navigation",
    "#nav",
    ".breadcrumbs",
    "#breadcrumbs",
    ".share",
    "#share",
    ".widget",
    "#widget",
    r#".cookie, #cookie, [aria-label="Cookie consent" i]"#,
];
// Firecrawl's forceIncludeMainTags, the one that applies outside its customers' own sites. An
// element holding it is kept whatever its class says, as a "#main" under a ".top" wrapper.
const FORCE_MAIN: &str = "main";
// Elements whose text is not prose, as BeautifulSoup's get_text leaves them out.
const NOT_TEXT: [&str; 5] = ["script", "style", "template", "rt", "rp"];

fn regex(pattern: &str) -> Regex {
    Regex::new(pattern).expect("a valid pattern")
}

// A footnote marker, a superscript "[12]". Not any bracket pair: a page's own "[1, 2]" or a
// transcription such as "[ˈkwɑsez̠]" is content.
static FOOTNOTE_MARK: LazyLock<Regex> = LazyLock::new(|| regex(r"^(?:\[[^\]]{1,8}\])+$"));
// The wiki edit links and tags, as markdown escapes them.
static WIKI_TAG: LazyLock<Regex> = LazyLock::new(|| regex(r"(?i)\\\[(?:edit|citation needed)\\\]"));
static PADDING: LazyLock<Regex> = LazyLock::new(|| regex(r" {2,}"));
// Where a link sits, from its ancestors' tags and from the class or id they carry.
static NAV_MARK: LazyLock<Regex> =
    LazyLock::new(|| regex(r"(?i)menu|navbar|navigation|breadcrumb"));
static FOOTER_MARK: LazyLock<Regex> = LazyLock::new(|| regex(r"(?i)footer"));
static RELATED_MARK: LazyLock<Regex> =
    LazyLock::new(|| regex(r"(?i)related|see-also|further-reading|recommended"));
// A section a link is under, however its box is marked. Matched at the start of the heading.
const RELATED_HEADINGS: [&str; 5] =
    ["see also", "related", "further reading", "external links", "you may also like"];

/// Where on its page a link sits.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Region {
    Main,
    Related,
    Nav,
    Footer,
}

impl Region {
    fn is_furniture(self) -> bool {
        matches!(self, Self::Nav | Self::Footer)
    }
}

/// A link of a page: the target as written, its anchor text, the text around it, and where on the
/// page it sits.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Link {
    pub url: String,
    pub anchor: String,
    pub context: String,
    pub region: Region,
}

/// What a page's HTML holds.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Static {
    pub title: String,
    pub text: String,
    pub links: Vec<Link>,
}

/// The article of `html`, with the title and links read before any of the page is dropped.
pub fn read_html(html: &str, final_url: &str) -> Static {
    let document = Document::from(html);
    let title = title(&document);
    let links = links(&document, final_url);
    let text = extract_document(&document);
    Static { title, text, links }
}

/// The article as markdown, without navigation and other page furniture.
pub fn extract(html: &str) -> String {
    extract_document(&Document::from(html))
}

fn extract_document(document: &Document) -> String {
    document.select(INTERACTIVE).remove();
    document.select(LINK_BOXES).remove();
    for sup in document.select("sup").nodes() {
        if FOOTNOTE_MARK.is_match(collapsed(sup).as_str()) {
            sup.remove_from_parent();
        }
    }
    let options = rs_trafilatura::Options {
        include_tables: true,
        include_comments: false,
        include_links: false,
        include_images: false,
        favor_recall: true,
        output_markdown: true,
        ..Default::default()
    };
    // It refuses a page it finds nothing in, which is what the cleaned page is for.
    let article = rs_trafilatura::extract_with_options(&document.html(), &options)
        .map(|result| result.content_markdown.unwrap_or(result.content_text))
        .map(|text| tidy(&text))
        .unwrap_or_default();
    if article.chars().count() >= MIN_TEXT { article } else { cleaned(document) }
}

/// The page's text once everything that is not its main content has been dropped.
fn cleaned(document: &Document) -> String {
    document.select(BOILERPLATE).remove();
    for selector in NON_MAIN {
        for node in document.select(selector).nodes() {
            let keep = node
                .descendants_it()
                .any(|inner| inner.id_attr().is_some_and(|id| &*id == FORCE_MAIN));
            if !keep {
                node.remove_from_parent();
            }
        }
    }
    let mut lines = Vec::new();
    for string in strings(&document.root()) {
        lines.extend(
            string.lines().map(str::trim).filter(|line| !line.is_empty()).map(String::from),
        );
    }
    lines.join("\n")
}

/// The markdown without wiki tags, and without the spaces that align a table's columns: one row can
/// carry thousands, and they count against a piece's size.
fn tidy(markdown: &str) -> String {
    let text = WIKI_TAG.replace_all(markdown, "");
    let lines: Vec<String> = text
        .split('\n')
        .map(|line| {
            if line.trim_start().starts_with('|') {
                PADDING.replace_all(line, " ").into_owned()
            } else {
                line.to_owned()
            }
        })
        .collect();
    lines.join("\n")
}

/// The text nodes under `node`, in order. Walked with a stack: a hostile page can nest deeper than
/// the call stack goes.
fn strings(node: &NodeRef) -> Vec<String> {
    let mut found = Vec::new();
    let mut stack = node.children();
    stack.reverse();
    while let Some(next) = stack.pop() {
        if next.is_text() {
            found.push(next.text().to_string());
        } else if next.is_element()
            && !next.node_name().is_some_and(|name| NOT_TEXT.contains(&&*name))
        {
            let mut children = next.children();
            children.reverse();
            stack.append(&mut children);
        }
    }
    found
}

fn collapsed(node: &NodeRef) -> String {
    strings(node).concat().split_whitespace().collect::<Vec<_>>().join(" ")
}

fn title(document: &Document) -> String {
    let title = document.select("title").first();
    let title = title.nodes().first().map(collapsed).unwrap_or_default();
    if !title.is_empty() {
        return title;
    }
    document.select("h1").first().nodes().first().map(collapsed).unwrap_or_default()
}

/// The page's links, one per target: the first outside `nav` and `footer`, else the first.
fn links(document: &Document, final_url: &str) -> Vec<Link> {
    let Ok(final_parsed) = Url::parse(final_url) else {
        return Vec::new();
    };
    let base = document
        .select("base[href]")
        .first()
        .attr("href")
        .and_then(|href| final_parsed.join(href.trim()).ok())
        .unwrap_or_else(|| final_parsed.clone());
    let own = normalize(final_url).ok();
    let mut found: Vec<Link> = Vec::new();
    let mut at: HashMap<String, usize> = HashMap::new();
    // In document order, so each link knows the last heading before it.
    let mut heading = String::new();
    for node in document.select("a[href], h2, h3, h4").nodes() {
        if node.node_name().as_deref() != Some("a") {
            heading = collapsed(node).to_lowercase();
            continue;
        }
        let href = node.attr("href").unwrap_or_default();
        let Ok(target) = base.join(href.trim()) else {
            continue;
        };
        if !matches!(target.scheme(), "http" | "https") {
            continue;
        }
        // Kept as written: a site may answer only under its www. or http spelling.
        let target = target.to_string();
        let Ok(key) = normalize(&target) else {
            continue;
        };
        if own.as_ref() == Some(&key) {
            continue;
        }
        let region = region(node, &heading);
        let link = || Link {
            url: target.clone(),
            anchor: collapsed(node),
            context: context(node),
            region,
        };
        match at.get(&key) {
            None => {
                at.insert(key, found.len());
                found.push(link());
            }
            Some(&i) if found[i].region.is_furniture() && !region.is_furniture() => {
                found[i] = link();
            }
            Some(_) => {}
        }
    }
    found
}

fn region(a: &NodeRef, heading: &str) -> Region {
    let (mut nav, mut footer, mut related) = (false, false, false);
    for parent in a.ancestors_it(None) {
        let Some(name) = parent.node_name() else {
            continue;
        };
        // The root's classes are feature flags, as Wikipedia's "main-menu-disabled", not a region.
        if matches!(&*name, "html" | "body") {
            continue;
        }
        let marks = format!(
            "{} {}",
            parent.id_attr().as_deref().unwrap_or_default(),
            parent.class().as_deref().unwrap_or_default()
        );
        let role = parent.attr("role");
        nav |= matches!(&*name, "nav" | "header")
            || role.as_deref() == Some("navigation")
            || NAV_MARK.is_match(&marks);
        footer |= &*name == "footer"
            || role.as_deref() == Some("contentinfo")
            || FOOTER_MARK.is_match(&marks);
        related |= &*name == "aside" || RELATED_MARK.is_match(&marks);
    }
    related |= RELATED_HEADINGS.iter().any(|start| heading.starts_with(start));
    if nav {
        Region::Nav
    } else if footer {
        Region::Footer
    } else if related {
        Region::Related
    } else {
        Region::Main
    }
}

fn context(a: &NodeRef) -> String {
    let block = a
        .ancestors_it(None)
        .find(|parent| {
            parent
                .node_name()
                .is_some_and(|name| matches!(&*name, "p" | "li" | "td" | "dd" | "figcaption"))
        })
        .or_else(|| a.parent())
        .unwrap_or(*a);
    let text = collapsed(&block);
    let size = text.chars().count();
    if size <= CONTEXT_CHARACTERS {
        return text;
    }
    let anchor = collapsed(a);
    let found = text.find(&anchor).map_or(0, |at| text[..at].chars().count());
    let middle = found + anchor.chars().count() / 2;
    let start = middle.saturating_sub(CONTEXT_CHARACTERS / 2).min(size - CONTEXT_CHARACTERS);
    text.chars().skip(start).take(CONTEXT_CHARACTERS).collect()
}
