//! The one spelling two URLs of a page share, as courlan's strict `normalize_url` writes it, and
//! the rest of what `fetch.normalize` did. Pages are keyed by it.

use std::collections::BTreeMap;
use std::net::Ipv6Addr;

/// A URL that cannot be read.
#[derive(Clone, Debug, thiserror::Error)]
#[error("invalid URL")]
pub struct BadUrl;

// courlan's strict list: every other query parameter is dropped.
const ALLOWED_PARAMS: [&str; 16] = [
    "aid",
    "article_id",
    "artnr",
    "id",
    "itemid",
    "objectid",
    "p",
    "page",
    "pagenum",
    "page_id",
    "pid",
    "post",
    "postid",
    "product_id",
    "lang",
    "language",
];

/// `url` in the form two spellings of one page share: https, no `www.`, no fragment, tracking
/// parameters, trailing slash or `index.html`. A `#/` or `#!/` route is kept: it names a page.
pub fn normalize(url: &str) -> Result<String, BadUrl> {
    let (url, route) = match route_start(url) {
        Some(at) => url.split_at(at),
        None => (url, ""),
    };
    let url = url.replace("&amp;", "&");
    let (scheme, netloc, path, query) = split_url(&url)?;
    let netloc = decode_punycode(&netloc.to_lowercase());
    let netloc = match scheme.as_str() {
        "http" => netloc.strip_suffix(":80").unwrap_or(&netloc),
        "https" => netloc.strip_suffix(":443").unwrap_or(&netloc),
        _ => &netloc,
    };
    let netloc = netloc.strip_prefix("www.").unwrap_or(netloc);
    let path = clean_path(path);
    let path = path.strip_suffix("index.html").unwrap_or(&path).trim_end_matches('/');
    let slash = if path.is_empty() || path.starts_with('/') { "" } else { "/" };
    let query = clean_query(query);
    let mark = if query.is_empty() { "" } else { "?" };
    Ok(format!("https://{netloc}{slash}{path}{mark}{query}{route}"))
}

fn route_start(url: &str) -> Option<usize> {
    url.match_indices('#')
        .find(|(at, _)| {
            let rest = &url[at + 1..];
            rest.starts_with('/') || rest.starts_with("!/")
        })
        .map(|(at, _)| at)
}

/// Scheme, host part, path and query, as `urllib.parse.urlsplit` cuts them.
fn split_url(url: &str) -> Result<(String, &str, &str, &str), BadUrl> {
    let url = url.trim_start_matches(|c: char| c <= ' ');
    let (scheme, rest) = match url.split_once(':') {
        Some((scheme, rest))
            if scheme.starts_with(|c: char| c.is_ascii_alphabetic())
                && scheme.chars().all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c)) =>
        {
            (scheme.to_ascii_lowercase(), rest)
        }
        _ => (String::new(), url),
    };
    let (netloc, rest) = match rest.strip_prefix("//") {
        Some(rest) => rest.split_at(rest.find(['/', '?', '#']).unwrap_or(rest.len())),
        None => ("", rest),
    };
    check_brackets(netloc)?;
    let rest = rest.split_once('#').map_or(rest, |(rest, _)| rest);
    let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
    Ok((scheme, netloc, path, query))
}

fn check_brackets(netloc: &str) -> Result<(), BadUrl> {
    let host = netloc.rsplit_once('@').map_or(netloc, |(_, host)| host);
    match (host.find('['), host.find(']')) {
        (None, None) => Ok(()),
        (Some(open), Some(close)) if open < close => {
            let inside = &host[open + 1..close];
            let zone = inside.split_once('%').map_or(inside, |(address, _)| address);
            zone.parse::<Ipv6Addr>().map(|_| ()).map_err(|_| BadUrl)
        }
        _ => Err(BadUrl),
    }
}

fn decode_punycode(host: &str) -> String {
    if !host.contains("xn--") {
        return host.to_owned();
    }
    let parts: Vec<String> = host
        .split('.')
        .map(|part| {
            if !part.starts_with("xn--") {
                return part.to_owned();
            }
            match idna::domain_to_unicode(part) {
                (decoded, Ok(())) => decoded,
                _ => part.to_owned(),
            }
        })
        .collect();
    parts.join(".")
}

fn clean_path(path: &str) -> String {
    let mut single = String::with_capacity(path.len());
    for c in path.chars() {
        if c != '/' || !single.ends_with('/') {
            single.push(c);
        }
    }
    // Leading /../'s go: nothing is above the root.
    let mut rest = single.as_str();
    while let Some(after) = rest.strip_prefix("/..") {
        if after.is_empty() || after.starts_with('/') {
            rest = after;
        } else {
            break;
        }
    }
    quote(rest, "/%!=:,-")
}

/// Query parameters on the allowed list, sorted, as `parse_qs` and `urlencode` leave them: one with
/// no value goes.
fn clean_query(query: &str) -> String {
    let mut kept: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for pair in query.split('&') {
        let Some((name, value)) = pair.split_once('=') else {
            continue;
        };
        if value.is_empty() {
            continue;
        }
        let name = unquote_plus(name);
        if ALLOWED_PARAMS.contains(&name.to_lowercase().as_str()) {
            kept.entry(name).or_default().push(unquote_plus(value));
        }
    }
    let mut pairs = Vec::new();
    for (name, values) in &kept {
        for value in values {
            pairs.push(format!("{}={}", quote_plus(name), quote_plus(value)));
        }
    }
    pairs.join("&")
}

fn quote(text: &str, safe: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        let c = char::from(byte);
        if c.is_ascii_alphanumeric() || "_.-~".contains(c) || safe.contains(c) {
            out.push(c);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

fn quote_plus(text: &str) -> String {
    quote(text, " ").replace(' ', "+")
}

fn unquote_plus(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut at = 0;
    while at < bytes.len() {
        let hex = (bytes[at] == b'%')
            .then(|| bytes.get(at + 1..at + 3))
            .flatten()
            .and_then(|hex| std::str::from_utf8(hex).ok())
            .filter(|hex| hex.bytes().all(|b| b.is_ascii_hexdigit()))
            .and_then(|hex| u8::from_str_radix(hex, 16).ok());
        if let Some(byte) = hex {
            out.push(byte);
            at += 3;
        } else {
            out.push(if bytes[at] == b'+' { b' ' } else { bytes[at] });
            at += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}
