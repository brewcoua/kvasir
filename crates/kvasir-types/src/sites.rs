//! What a site is: its host key and the kinds and tags a source can carry.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

/// Source kinds in priority order: a host that several items classify takes the first.
pub const KINDS: [&str; 8] = [
    "intergovernmental",
    "government",
    "academic",
    "standards",
    "news",
    "reference",
    "organization",
    "unknown",
];

/// The tags a list or the bundled file may set.
pub const LIST_TAGS: [&str; 3] = ["ai_generated", "content_farm", "predatory"];

/// What a downloaded list may be labelled: a kind other than `unknown`, or a tag.
pub fn is_list_label(label: &str) -> bool {
    KINDS[..KINDS.len() - 1].contains(&label) || LIST_TAGS.contains(&label)
}

/// The kinds that make a site `high` on their own.
const HIGH_KINDS: [&str; 4] = ["intergovernmental", "government", "academic", "standards"];
const AVOID_TAGS: [&str; 5] =
    ["ai_generated", "content_farm", "predatory", "rsp_deprecated", "deny"];

/// How far a site is trusted.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum Tier {
    High,
    Normal,
    Unknown,
    Low,
    Avoid,
}

/// The tier of a kind and its tags, by the first rule that fits.
pub fn tier<'a>(kind: &str, tags: impl IntoIterator<Item = &'a str> + Clone) -> Tier {
    let has = |wanted: &[&str]| tags.clone().into_iter().any(|tag| wanted.contains(&tag));
    if has(&AVOID_TAGS) {
        Tier::Avoid
    } else if has(&["rsp_unreliable"]) {
        Tier::Low
    } else if HIGH_KINDS.contains(&kind) || has(&["rsp_reliable", "allow"]) {
        Tier::High
    } else if kind == "unknown" {
        Tier::Unknown
    } else {
        Tier::Normal
    }
}

/// What is known of a site: its kind, tags and trust tier.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
pub struct SourceInfo {
    pub kind: String,
    pub tags: Vec<String>,
    pub tier: Tier,
    /// Empty when Wikidata gave none.
    pub label: String,
    /// The Wikidata item, or empty.
    pub qid: String,
    /// What decided it: config, mark, list, rule or wikidata. Empty when nothing is known. A site
    /// set in the config cannot be marked.
    pub origin: String,
}

/// The domain `host` was registered under, which a private suffix such as `github.io` counts for.
/// `host` itself when its suffix is not on the public suffix list.
pub fn registrable(host: &str) -> &str {
    match psl::domain(host.as_bytes()) {
        Some(domain) if domain.suffix().is_known() => &host[host.len() - domain.as_bytes().len()..],
        _ => host,
    }
}

/// `raw` as the key a site is cached under: lowercase, no `www.` and no port. `None` for an IP
/// literal or anything that is not a DNS name with a dot.
///
/// Non-ASCII names go through UTS 46, where Python used IDNA 2003. The two differ on a few
/// characters, such as `ß`, which IDNA 2003 maps to `ss`.
pub fn normalize_host(raw: &str) -> Option<String> {
    let host = raw.trim().to_lowercase();
    if host.matches(':').count() > 1 || host.starts_with('[') {
        return None;
    }
    let (host, port) = host.split_once(':').unwrap_or((&host, ""));
    if !port.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let host = if host.is_ascii() { host.to_owned() } else { idna::domain_to_ascii(host).ok()? };
    let host = host.strip_prefix("www.").unwrap_or(&host);
    let labels: Vec<&str> = host.split('.').collect();
    let last = labels[labels.len() - 1];
    // A name never ends in digits only, which also keeps out 1.2.3.4 and 127.1.
    if host.len() > 253 || labels.len() < 2 || last.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    labels.iter().all(|label| is_label(label)).then(|| host.to_owned())
}

fn is_label(label: &str) -> bool {
    let bytes = label.as_bytes();
    (1..=63).contains(&bytes.len())
        && bytes.iter().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-')
        && bytes[0] != b'-'
        && bytes[bytes.len() - 1] != b'-'
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hosts() {
        for (raw, want) in [
            (" WWW.Example.ORG ", Some("example.org")),
            ("a.example:443", Some("a.example")),
            ("a.example:", Some("a.example")),
            ("bücher.example", Some("xn--bcher-kva.example")),
            ("a.example:x", None),
            ("[::1]", None),
            ("::1", None),
            ("1.2.3.4", None),
            ("127.1", None),
            ("localhost", None),
            ("under_score.example", None),
            ("-a.example", None),
            ("a..example", None),
            ("a.example.", None),
            ("", None),
        ] {
            assert_eq!(normalize_host(raw).as_deref(), want, "{raw:?}");
        }
    }

    #[test]
    fn registrable_domains() {
        for (host, want) in [
            ("a.example.org", "example.org"),
            ("a.b.example.co.uk", "example.co.uk"),
            ("example.org", "example.org"),
            ("someone.github.io", "someone.github.io"),
            ("a.someone.github.io", "someone.github.io"),
            ("a.b.not-a-suffix", "a.b.not-a-suffix"),
            ("org", "org"),
        ] {
            assert_eq!(registrable(host), want, "{host}");
        }
    }

    #[test]
    fn tiers_follow_the_first_rule_that_fits() {
        for (kind, tags, want) in [
            ("organization", &["ai_generated"][..], Tier::Avoid),
            ("news", &["content_farm"], Tier::Avoid),
            ("government", &["predatory"], Tier::Avoid),
            ("reference", &["rsp_deprecated"], Tier::Avoid),
            ("unknown", &["deny"], Tier::Avoid),
            ("news", &["rsp_unreliable"], Tier::Low),
            ("government", &["rsp_unreliable"], Tier::Low),
            ("government", &[], Tier::High),
            ("intergovernmental", &[], Tier::High),
            ("academic", &["official"], Tier::High),
            ("standards", &[], Tier::High),
            ("news", &["rsp_reliable"], Tier::High),
            ("unknown", &["allow"], Tier::High),
            ("news", &[], Tier::Normal),
            ("reference", &["community", "official"], Tier::Normal),
            ("organization", &["official"], Tier::Normal),
            ("unknown", &[], Tier::Unknown),
        ] {
            assert_eq!(tier(kind, tags.iter().copied()), want, "{kind} {tags:?}");
        }
    }

    #[test]
    fn labels() {
        assert!(is_list_label("government"));
        assert!(is_list_label("predatory"));
        assert!(!is_list_label("unknown"));
        assert!(!is_list_label("rsp_reliable"));
    }
}
