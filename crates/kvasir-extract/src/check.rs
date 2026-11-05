//! Whether static HTML holds an article, or a challenge, a block page or an empty shell.

use std::sync::LazyLock;

use regex::Regex;

use crate::head;

fn regex(pattern: &str) -> Regex {
    Regex::new(pattern).expect("a valid pattern")
}

// Crawl4AI's tier 1 markers: only a block or challenge page holds them, so they count at any size.
static CHALLENGE: LazyLock<Regex> = LazyLock::new(|| {
    regex(concat!(
        r"(?is)Reference\s*#\s*\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+|Pardon\s+Our\s+Interruption",
        r#"|challenge-form.*?__cf_chl_f_tk=|<span\s+class="cf-error-code">\d{4}</span>"#,
        r"|/cdn-cgi/challenge-platform/\S+orchestrate|window\._pxAppId\s*=|captcha\.px-cdn\.net",
        r"|captcha-delivery\.com|_Incapsula_Resource|Incapsula\s+incident\s+ID",
        r"|Sucuri\s+WebSite\s+Firewall|KPSDK\.scriptStart\s*=\s*KPSDK\.now\(\)",
        r"|blocked\s+by\s+network\s+security",
    ))
});
// Tier 2: phrases that also occur in articles and login forms, so only a short page is judged by
// them.
static BLOCK_PHRASE: LazyLock<Regex> = LazyLock::new(|| {
    regex(concat!(
        r"(?i)Access\s+Denied|Checking\s+your\s+browser|<title>\s*Just\s+a\s+moment",
        r#"|class=["'](?:g-recaptcha|h-captcha)["']|Access\s+to\s+This\s+Page\s+Has\s+Been\s+Blocked"#,
        r"|blocked\s+by\s+security|Request\s+unsuccessful",
    ))
});
const MAX_BLOCK_PHRASE: usize = 10_000;
// The titles FlareSolverr looks for to know it is facing a challenge.
static BLOCK_TITLE: LazyLock<Regex> = LazyLock::new(|| {
    regex(
        r"(?i)<title>\s*(?:Just a moment\.\.\.|DDoS-Guard|Access denied|Attention Required! \| Cloudflare)",
    )
});
// Crawl4AI's tier 3: a page this small with two signs of having no content is an empty shell, one
// sign is enough when it is smaller still.
const MAX_SHELL: usize = 50_000;
const MAX_SHELL_ONE_SIGN: usize = 5_000;
static BODY: LazyLock<Regex> = LazyLock::new(|| regex(r"(?i)<body\b"));
static CONTENT_ELEMENT: LazyLock<Regex> =
    LazyLock::new(|| regex(r"(?i)<(?:p|h[1-6]|article|section|li|td|a|pre)\b"));
static SCRIPT: LazyLock<Regex> = LazyLock::new(|| regex(r"(?i)<script\b"));
static SCRIPT_OR_STYLE: LazyLock<Regex> =
    LazyLock::new(|| regex(r"(?is)<script\b.*?</script>|<style\b.*?</style>"));
static TAG: LazyLock<Regex> = LazyLock::new(|| regex(r"<[^>]+>"));
// HTML parsing and extraction take time quadratic in the nesting, and 1,000,000 characters of
// "<div>" would hold a thread for hours. At this depth a page takes under a second. Pages nest in
// the hundreds at most.
const MAX_DEPTH: usize = 2000;
static TAG_NAME: LazyLock<Regex> = LazyLock::new(|| regex(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)"));
// Tags that are not closed, or that the parser closes for itself, so they do not nest.
const NOT_NESTING: [&str; 26] = [
    "area", "base", "br", "col", "colgroup", "dd", "dt", "embed", "hr", "img", "input", "li",
    "link", "meta", "option", "p", "param", "source", "tbody", "td", "tfoot", "th", "thead", "tr",
    "track", "wbr",
];

/// Why a static page holds no article.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reason {
    /// A block or challenge page.
    Challenge,
    /// A 403 or 503.
    Blocked,
    /// A page whose content a browser would have to build.
    Shell,
    /// Nested too deep to parse in reasonable time.
    Nested,
}

/// A static page that holds no article.
#[derive(Clone, Debug, thiserror::Error)]
#[error("{message}")]
pub struct Unusable {
    pub message: String,
    pub reason: Reason,
}

/// Fails when static `html` is a challenge, a block page or an empty shell.
pub fn check_page(url: &str, html: &str) -> Result<(), Unusable> {
    if deepest(html) > MAX_DEPTH {
        return Err(Unusable {
            message: format!("{url}: nested over {MAX_DEPTH} elements deep"),
            reason: Reason::Nested,
        });
    }
    let size = html.chars().count();
    // Block pages bury their text under inline scripts and styles, so those are cut for the scan.
    let scrubbed = SCRIPT_OR_STYLE.replace_all(head(html, 500_000), "");
    let bare = head(&scrubbed, 30_000);
    let found = CHALLENGE
        .find(head(html, 15_000))
        .or_else(|| CHALLENGE.find(bare))
        .or_else(|| BLOCK_TITLE.find(html))
        .or_else(|| (size < MAX_BLOCK_PHRASE).then(|| BLOCK_PHRASE.find(html)).flatten());
    if let Some(found) = found {
        return Err(Unusable {
            message: format!(
                "{url}: challenge or block page ({})",
                head(found.as_str().trim(), 40)
            ),
            reason: Reason::Challenge,
        });
    }
    if size > MAX_SHELL {
        return Ok(());
    }
    let without_scripts = SCRIPT_OR_STYLE.replace_all(html, "");
    let without_tags = TAG.replace_all(&without_scripts, "");
    let visible = without_tags.trim().chars().count();
    let content = CONTENT_ELEMENT.find_iter(html).count();
    let no_body = !BODY.is_match(html);
    let mut signs = Vec::new();
    if no_body {
        signs.push("no body");
    }
    if visible < 50 {
        signs.push("little text");
    }
    if content == 0 {
        signs.push("no content elements");
    }
    if content == 0 && visible < 100 && SCRIPT.is_match(html) {
        signs.push("scripts only");
    }
    if no_body || signs.len() >= 2 || (!signs.is_empty() && size < MAX_SHELL_ONE_SIGN) {
        return Err(Unusable {
            message: format!("{url}: empty page shell ({})", signs.join(", ")),
            reason: Reason::Shell,
        });
    }
    Ok(())
}

/// How deep the tags of `html` nest, counted from its text so nothing is parsed first.
fn deepest(html: &str) -> usize {
    let (mut depth, mut deepest) = (0usize, 0);
    for tag in TAG_NAME.captures_iter(html) {
        if NOT_NESTING.contains(&tag[2].to_ascii_lowercase().as_str()) {
            continue;
        }
        if tag[1].is_empty() {
            depth += 1;
            deepest = deepest.max(depth);
        } else {
            depth = depth.saturating_sub(1);
        }
    }
    deepest
}
