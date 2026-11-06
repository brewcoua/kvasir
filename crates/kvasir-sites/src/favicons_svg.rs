//! Keeps of an SVG only what draws it. Sites and providers are not trusted, and the file is served
//! from the dashboard's own origin, so this is an allowlist: anything it does not name is dropped,
//! with what is inside it. No script, event handler, external reference, entity or foreign content
//! gets through, and editors' metadata goes too, which is most of what a small SVG weighs.

use quick_xml::Reader;
use quick_xml::events::{BytesStart, Event};

const BOM: &[u8] = b"\xef\xbb\xbf";
const NAMESPACE: &str = "http://www.w3.org/2000/svg";
const MAX_DEPTH: usize = 64;
const DRAWN: [&str; 9] =
    ["path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "use"];
const ELEMENTS: [&str; 20] = [
    "svg",
    "g",
    "defs",
    "symbol",
    "use",
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "text",
    "tspan",
    "linearGradient",
    "radialGradient",
    "stop",
    "clipPath",
    "mask",
    "style",
];
const ATTRIBUTES: [&str; 57] = [
    "id",
    "class",
    "style",
    "media",
    "viewBox",
    "preserveAspectRatio",
    "width",
    "height",
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "fx",
    "fy",
    "dx",
    "dy",
    "d",
    "points",
    "pathLength",
    "transform",
    "transform-origin",
    "href",
    "gradientUnits",
    "gradientTransform",
    "spreadMethod",
    "offset",
    "stop-color",
    "stop-opacity",
    "clip-path",
    "clip-rule",
    "clipPathUnits",
    "mask",
    "maskUnits",
    "maskContentUnits",
    "fill",
    "fill-opacity",
    "fill-rule",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-miterlimit",
    "stroke-dasharray",
    "stroke-dashoffset",
    "stroke-opacity",
    "opacity",
    "color",
    "font-family",
    "font-size",
    "text-anchor",
];

/// The SVG with only what is safe to serve, or None when `body` is not one or nothing of it draws.
pub(super) fn clean(body: &[u8]) -> Option<Vec<u8>> {
    let text = std::str::from_utf8(body.strip_prefix(BOM).unwrap_or(body)).ok()?;
    let mut reader = Reader::from_str(text);
    let mut out = String::with_capacity(text.len());
    let mut open: Vec<&str> = Vec::new();
    // How deep inside a dropped element this is.
    let mut dropped = 0usize;
    // A style's text, whole: a comment between two pieces could otherwise split a rule the check
    // looks for.
    let mut style = String::new();
    let mut began = false;
    let mut draws = false;
    loop {
        let event = reader.read_event().ok()?;
        if dropped > 0 {
            match event {
                Event::Start(_) => dropped += 1,
                Event::End(_) => dropped -= 1,
                Event::Eof => return None,
                _ => {}
            }
            continue;
        }
        match event {
            Event::Eof => break,
            Event::Start(_) | Event::Empty(_) if open.is_empty() && began => return None,
            Event::Start(tag) => {
                let name = name_of(&tag, began)?;
                began = true;
                let Some(name) = ELEMENTS.iter().copied().find(|kept| *kept == name) else {
                    dropped = 1;
                    continue;
                };
                if open.len() == MAX_DEPTH {
                    return None;
                }
                draws |= DRAWN.contains(&name);
                write_tag(&mut out, name, &tag, open.is_empty(), false)?;
                open.push(name);
            }
            Event::Empty(tag) => {
                let name = name_of(&tag, began)?;
                began = true;
                if ELEMENTS.contains(&name) {
                    draws |= DRAWN.contains(&name);
                    write_tag(&mut out, name, &tag, open.is_empty(), true)?;
                }
            }
            Event::End(_) => {
                let name = open.pop()?;
                if name == "style" {
                    if safe_css(&style) {
                        out.push_str(&style.replace('&', "&amp;").replace('<', "&lt;"));
                    }
                    style.clear();
                }
                out.push_str("</");
                out.push_str(name);
                out.push('>');
            }
            Event::Text(text) => match open.last() {
                Some(&"style") => style.push_str(&text),
                Some(&("text" | "tspan")) => out.push_str(&text),
                _ => {}
            },
            Event::CData(text) if open.last() == Some(&"style") => {
                style.push_str(&text);
            }
            // No DOCTYPE is kept, so a reference to any entity it declared would not parse.
            Event::GeneralRef(reference)
                if matches!(open.last(), Some(&("text" | "tspan")))
                    && (reference.is_char_ref()
                        || matches!(&*reference, "amp" | "lt" | "gt" | "quot" | "apos")) =>
            {
                out.push('&');
                out.push_str(&reference);
                out.push(';');
            }
            _ => {}
        }
    }
    (open.is_empty() && draws).then(|| out.into_bytes())
}

/// The name of an element, which for the first one of the file must be `svg`.
fn name_of<'a>(tag: &'a BytesStart<'_>, began: bool) -> Option<&'a str> {
    let name = tag.name().0;
    (began || name == "svg").then_some(name)
}

fn write_tag(
    out: &mut String,
    name: &str,
    tag: &BytesStart<'_>,
    root: bool,
    empty: bool,
) -> Option<()> {
    out.push('<');
    out.push_str(name);
    if root {
        out.push_str(&format!(" xmlns=\"{NAMESPACE}\""));
    }
    let mut linked = false;
    for attribute in tag.attributes() {
        let attribute = attribute.ok()?;
        let key = attribute.key.0;
        // The older spelling needs a namespace declared, and browsers all read the newer one.
        let key = if key == "xlink:href" { "href" } else { key };
        let value = &*attribute.value;
        if !ATTRIBUTES.contains(&key) || !safe_value(key, value) || (key == "href" && linked) {
            continue;
        }
        linked |= key == "href";
        out.push_str(&format!(" {key}=\"{}\"", value.replace('"', "&quot;")));
    }
    out.push_str(if empty { "/>" } else { ">" });
    Some(())
}

// An entity could spell anything the checks look for, and no drawing needs one in an attribute.
fn safe_value(key: &str, value: &str) -> bool {
    !value.contains(['&', '<']) && safe_css(value) && (key != "href" || value.starts_with('#'))
}

/// No way out of the file: a `url()` may name only something in it.
fn safe_css(css: &str) -> bool {
    let css = css.to_lowercase();
    let barred = ["@import", "expression(", "javascript:", "behavior:", "-moz-binding", "\\"];
    !barred.iter().any(|word| css.contains(word))
        && css
            .split("url(")
            .skip(1)
            .all(|target| target.trim_start().trim_start_matches(['"', '\'']).starts_with('#'))
}
