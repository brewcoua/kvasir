//! Extraction against saved pages: what must survive, what must go, and what is not a page.

use std::fs;
use std::path::PathBuf;

use kvasir_extract::{Reason, Region, check_page, extract, normalize, read_html, split};

fn pages() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/pages")
}

fn page(file: &str) -> String {
    fs::read_to_string(pages().join(file)).unwrap()
}

fn catalog() -> Vec<toml::Value> {
    let text = fs::read_to_string(pages().join("pages.toml")).unwrap();
    let value: toml::Value = toml::from_str(&text).unwrap();
    value["page"].as_array().unwrap().clone()
}

fn collapse(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn strings(page: &toml::Value, key: &str) -> Vec<String> {
    page[key].as_array().unwrap().iter().map(|s| s.as_str().unwrap().to_owned()).collect()
}

#[test]
fn extract_keeps_the_content_and_drops_the_furniture() {
    let mut missed = Vec::new();
    for entry in catalog().iter().filter(|p| p["kind"].as_str() == Some("article")) {
        let file = entry["file"].as_str().unwrap();
        let text = collapse(&extract(&page(file)));
        for must in strings(entry, "must") {
            if !text.contains(&collapse(&must)) {
                missed.push(format!("{file} lost: {must}"));
            }
        }
        for must_not in strings(entry, "must_not") {
            if text.contains(&collapse(&must_not)) {
                missed.push(format!("{file} kept: {must_not}"));
            }
        }
    }
    assert!(missed.is_empty(), "{missed:#?}");
}

#[test]
fn a_shell_or_challenge_page_is_not_read_as_text() {
    for entry in catalog().iter().filter(|p| p["kind"].as_str() != Some("article")) {
        let file = entry["file"].as_str().unwrap();
        let found = check_page(entry["url"].as_str().unwrap(), &page(file)).unwrap_err();
        let expected =
            if entry["kind"].as_str() == Some("shell") { Reason::Shell } else { Reason::Challenge };
        assert_eq!(found.reason, expected, "{file}");
    }
}

#[test]
fn an_article_is_not_a_challenge() {
    for entry in catalog().iter().filter(|p| p["kind"].as_str() == Some("article")) {
        let file = entry["file"].as_str().unwrap();
        check_page(entry["url"].as_str().unwrap(), &page(file)).unwrap();
    }
}

#[test]
fn footnote_markers_are_removed() {
    let out = extract(&page("wiki_kvasir.html"));
    assert!(!out.contains("<sup>"));
    assert!(!regex::Regex::new(r"\\\[[0-9a-z]+\\\]").unwrap().is_match(&out), "{out}");
    assert!(out.contains(
        "Extremely wise, Kvasir traveled far and wide, teaching and spreading knowledge."
    ));
}

#[test]
fn lists_of_neighbouring_articles_are_dropped() {
    let out = extract(&page("wiki_kvasir.html"));
    assert!(!out.contains("Hidden categories"));
    assert!(!out.contains("List of dwarfs in Norse mythology"));
}

#[test]
fn a_table_row_carries_no_padding() {
    let out = extract(&page("wiki_bisection.html"));
    let row = out.lines().find(|line| line.starts_with('|') && line.matches('|').count() > 3);
    assert!(!row.unwrap().contains("  "));
}

#[test]
fn a_bracketed_span_of_the_page_itself_is_kept() {
    assert!(extract(&page("wiki_kvasir.html")).contains("ˈkwɑsez̠"));
    assert!(extract(&page("wiki_bisection.html")).contains(r"\[1, 2\]"));
}

#[test]
fn the_cleaned_page_is_read_when_extraction_returns_little() {
    let html =
        "<body><nav>Home</nav><main><p>Short note.</p></main><footer>Contact</footer></body>";
    assert_eq!(extract(html), "Short note.");
}

#[test]
fn an_element_holding_main_survives_the_fallback() {
    let html = r#"<body><div class="top"><div id="main"><p>Kept.</p></div></div><div class="ad">Gone</div></body>"#;
    assert_eq!(extract(html), "Kept.");
}

#[test]
fn a_page_nested_past_the_limit_is_refused_before_parsing() {
    let html = format!("<body>{}<p>{}</p></body>", "<div>".repeat(2001), "Deep. ".repeat(20));
    let found = check_page("https://example.org/a", &html).unwrap_err();
    assert_eq!(found.reason, Reason::Nested);
}

#[test]
fn a_page_nested_to_the_limit_is_read_on_a_small_stack() {
    let html = format!("<body>{}<p>{}</p></body>", "<div>".repeat(1999), "Deep. ".repeat(20));
    check_page("https://example.org/a", &html).unwrap();
    // The stack of a `spawn_blocking` thread, in a build whose frames are at their largest.
    let reader = std::thread::Builder::new().stack_size(2 << 20);
    let handle = reader.spawn(move || read_html(&html, "https://example.org/a")).unwrap();
    assert!(handle.join().unwrap().text.contains("Deep."));
}

#[test]
fn unclosed_paragraphs_do_not_count_as_nesting() {
    let html = format!("<html><body>{}</body></html>", "<p>A line of text.<br>".repeat(5000));
    check_page("https://example.org/a", &html).unwrap();
}

const SELF: &str = "https://example.org/a";

fn links_of(body: &str) -> Vec<kvasir_extract::Link> {
    read_html(body, SELF).links
}

fn urls(body: &str) -> Vec<String> {
    links_of(body).into_iter().map(|link| link.url).collect()
}

fn wiki(file: &str, url: &str) -> kvasir_extract::Static {
    read_html(&page(file), url)
}

#[test]
fn a_see_also_link_is_related_and_a_menu_link_is_nav() {
    let read = wiki("wiki_bisection.html", "https://en.wikipedia.org/wiki/Bisection_method");
    let find = |url: &str| read.links.iter().find(|link| link.url == url).unwrap();

    assert_eq!(
        find("https://en.wikipedia.org/wiki/Binary_search_algorithm").region,
        Region::Related
    );
    assert_eq!(find("https://en.wikipedia.org/wiki/Main_Page").region, Region::Nav);
    let mathematics = find("https://en.wikipedia.org/wiki/Mathematics");
    assert_eq!(mathematics.region, Region::Main);
    assert!(mathematics.context.contains("bisection method"));
    assert_eq!(read.title, "Bisection method - Wikipedia");
}

#[test]
fn the_root_classes_of_wikipedia_do_not_make_every_link_nav() {
    let read = wiki("wiki_kvasir.html", "https://en.wikipedia.org/wiki/Kvasir");
    assert!(read.links.iter().any(|link| link.region == Region::Main));
    assert!(read.links.iter().any(|link| link.region == Region::Related));
}

#[test]
fn the_menu_and_the_footer_of_a_cookie_wall_page_are_marked() {
    let read = read_html(&page("cookie_wall.html"), SELF);
    let region = |anchor: &str| read.links.iter().find(|l| l.anchor == anchor).map(|l| l.region);
    assert_eq!(region("Space"), Some(Region::Nav));
    assert_eq!(region("Privacy policy"), Some(Region::Footer));
    assert!(region("Sign in").is_some());
}

#[test]
fn normalize_answers_as_python_did() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/normalize.json");
    let cases: serde_json::Map<String, serde_json::Value> =
        serde_json::from_str(&fs::read_to_string(root).unwrap()).unwrap();
    let wrong: Vec<_> = cases
        .iter()
        .filter_map(|(url, expected)| {
            let got = normalize(url).unwrap();
            (got != expected.as_str().unwrap()).then(|| format!("{url}: {got} != {expected}"))
        })
        .collect();
    assert!(wrong.is_empty(), "{wrong:#?}");
}

#[test]
fn normalize_refuses_a_bad_host() {
    for url in ["https://[::1/a", "https://::1]/a", "https://[zz]/a"] {
        assert!(normalize(url).is_err(), "{url}");
    }
}

#[test]
fn relative_links_resolve_against_the_final_url() {
    let found = read_html(
        r#"<body><p><a href="../b/c">C</a> <a href="/d">D</a></p></body>"#,
        "https://example.org/x/y",
    );
    let urls: Vec<_> = found.links.iter().map(|link| link.url.as_str()).collect();
    assert_eq!(urls, ["https://example.org/b/c", "https://example.org/d"]);
}

#[test]
fn a_base_element_changes_what_relative_links_resolve_against() {
    let html = r#"<head><base href="https://cdn.example.net/x/"></head><body><p><a href="y">Y</a></p></body>"#;
    assert_eq!(urls(html), ["https://cdn.example.net/x/y"]);
}

#[test]
fn links_that_are_not_pages_are_dropped() {
    let html = concat!(
        "<body><p>",
        r##"<a href="#top">top</a> <a href="javascript:void(0)">js</a> <a href="mailto:a@b.c">mail</a>"##,
        r##" <a href="/a?utm_medium=x#end">self</a> <a href="ftp://h/f">ftp</a> <a href="/ok">ok</a>"##,
        "</p></body>"
    );
    assert_eq!(urls(html), ["https://example.org/ok"]);
}

#[test]
fn a_target_keeps_its_first_link_outside_nav_and_footer() {
    let links = links_of(concat!(
        r#"<body><nav><a href="/x">Menu X</a></nav><p>See <a href="/x">the X</a>.</p>"#,
        r#"<p><a href="/y">first Y</a> <a href="/y">second Y</a></p>"#,
        r#"<footer><a href="/z">Foot Z</a></footer></body>"#,
    ));
    assert_eq!((links[0].anchor.as_str(), links[0].region), ("the X", Region::Main));
    assert_eq!(links[1].anchor, "first Y");
    assert_eq!(links[2].region, Region::Footer);
}

#[test]
fn a_link_under_a_related_heading_or_box_is_related() {
    let links = links_of(concat!(
        r#"<body><p><a href="/m">m</a></p><h2>Related articles</h2>"#,
        r#"<ul><li><a href="/b">b</a></li></ul>"#,
        r#"<h3>Notes</h3><p><a href="/c">c</a></p><div class="Recommended-posts"><a href="/d">d</a>"#,
        r#"</div><aside><a href="/e">e</a></aside></body>"#,
    ));
    let regions: Vec<_> = links.iter().map(|link| link.region).collect();
    assert_eq!(
        regions,
        [Region::Main, Region::Related, Region::Main, Region::Related, Region::Related]
    );
}

#[test]
fn context_is_the_block_around_the_anchor_cut_to_200_characters() {
    let long = "word ".repeat(100);
    let links = links_of(&format!(
        r#"<body><p>{long}<a href="/t">the target</a> {long}</p><ul><li>Short <a href="/s">item</a></li></ul></body>"#
    ));
    assert_eq!(links[0].context.chars().count(), 200);
    assert!(links[0].context.contains("the target"));
    assert_eq!(links[1].context, "Short item");
}

#[test]
fn the_title_falls_back_to_the_first_h1() {
    assert_eq!(read_html("<body><h1>The  stone</h1><p>x</p></body>", SELF).title, "The stone");
    assert_eq!(read_html("<title>T</title><body><h1>H</h1></body>", SELF).title, "T");
}

fn line_split(text: &str, size: usize) -> Vec<String> {
    let (mut pieces, mut current) = (Vec::new(), String::new());
    for line in text.lines() {
        let mut line: Vec<char> = line.chars().collect();
        while line.len() > size {
            if !current.is_empty() {
                pieces.push(std::mem::take(&mut current));
            }
            pieces.push(line[..size].iter().collect());
            line.drain(..size);
        }
        if !current.is_empty() && current.chars().count() + 1 + line.len() > size {
            pieces.push(std::mem::take(&mut current));
        }
        if !current.is_empty() {
            current.push('\n');
        }
        current.extend(line);
    }
    if !current.is_empty() {
        pieces.push(current);
    }
    pieces
}

const SECTIONED: [&str; 3] =
    ["wiki_bisection.html", "wiki_kvasir.html", "fastapi_first_steps.html"];
const SIZES: [usize; 3] = [800, 2000, 6000];

fn is_heading(line: &str) -> bool {
    let level = line.bytes().take_while(|b| *b == b'#').count();
    (1..=6).contains(&level)
        && line[level..].starts_with(' ')
        && line[level + 1..].starts_with(|c: char| !c.is_whitespace())
}

fn top_mark(text: &str) -> String {
    let levels: Vec<usize> = text
        .lines()
        .filter(|line| is_heading(line))
        .map(|line| line.bytes().take_while(|b| *b == b'#').count())
        .collect();
    let top = levels
        .iter()
        .copied()
        .filter(|level| levels.iter().filter(|other| *other == level).count() > 1)
        .min()
        .unwrap();
    format!("{} ", "#".repeat(top))
}

#[test]
fn split_keeps_pieces_in_size_and_in_one_top_level_section() {
    for name in SECTIONED {
        let text = extract(&page(name));
        let mark = top_mark(&text);
        for size in SIZES {
            let pieces = split(&text, size);
            for piece in &pieces {
                assert!(piece.chars().count() <= size, "{name} {size}");
                assert!(
                    piece.lines().filter(|l| l.starts_with(&mark)).count() <= 1,
                    "{name} {size}"
                );
            }
            let first =
                pieces.iter().position(|p| p.lines().any(|l| l.starts_with(&mark))).unwrap();
            assert!(pieces[first..].iter().all(|p| is_heading(p.lines().next().unwrap())));
        }
    }
}

#[test]
fn split_loses_no_line_of_the_text() {
    for name in SECTIONED {
        let text = extract(&page(name));
        for size in SIZES {
            let pieces = split(&text, size);
            let mut kept = pieces.iter().flat_map(|piece| piece.lines());
            for line in text.lines() {
                if !line.trim().is_empty() && line.chars().count() <= size {
                    // In order, once the headings added to a piece are skipped over.
                    assert!(kept.any(|other| other == line), "{name} {size}: {line}");
                }
            }
        }
    }
}

#[test]
fn split_prefixes_a_piece_cut_from_a_section_with_the_headings_above_it() {
    let paragraphs = vec!["p".repeat(40); 4].join("\n\n");
    let text = format!(
        "# Page\n\nabout\n\n## History\n\nlead\n\n### Principate\n\n{paragraphs}\n\n## Legacy\n\nlater"
    );
    let pieces = split(&text, 120);

    assert_eq!(pieces[0], "# Page\n\nabout");
    assert!(pieces[1].starts_with("## History\n\nlead\n\n### Principate\n\n"));
    assert!(pieces[2].starts_with("## History\n### Principate\n"));
    assert!(pieces.iter().all(|piece| piece.chars().count() <= 120));
    let legacy: Vec<_> = pieces.iter().filter(|piece| piece.contains("## Legacy")).collect();
    assert_eq!(legacy, ["## Legacy\n\nlater"]);
}

#[test]
fn split_cuts_a_section_over_the_size_between_paragraphs() {
    let (a, b, c) = ("a".repeat(30), "b".repeat(30), "c".repeat(30));
    let text = format!("## A\n\n{a}\n\n{b}\n\n{c}\n\n## B\n\nx");
    let pieces = split(&text, 80);

    assert_eq!(pieces[0], format!("## A\n\n{a}\n\n{b}"));
    assert_eq!(pieces[1], format!("## A\n{c}"));
    assert_eq!(pieces[2], "## B\n\nx");
}

#[test]
fn split_without_headings_is_the_line_split() {
    let text = format!("aaa\nbbb\n\ncc\n{}\n#not a heading\nee", "d".repeat(25));
    for size in [7, 10, 100] {
        assert_eq!(split(&text, size), line_split(&text, size), "{size}");
    }
}
