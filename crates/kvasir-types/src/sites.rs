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

/// Whether `effect` is what a list can do: `block`, `demote`, `exempt`, `trust`, or `kind:<kind>`.
pub fn is_list_effect(effect: &str) -> bool {
    matches!(effect, "block" | "demote" | "exempt" | "trust")
        || effect.strip_prefix("kind:").is_some_and(|kind| KINDS[..KINDS.len() - 1].contains(&kind))
}

/// Whether a list may give sites `tag`: a label, which must not set a tier by itself. Empty is none.
pub fn is_list_tag(tag: &str) -> bool {
    let word = tag.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
    let reserved = AVOID_TAGS.contains(&tag)
        || ["allow", "blocked", "demoted", "exempt", "official", "community", "likely_farm"]
            .contains(&tag)
        || tag.starts_with("rsp_");
    word && !reserved
}

/// A list of hosts, from the catalog or the operator's own.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
pub struct SourceList {
    pub id: String,
    pub name: String,
    /// Where it is downloaded from. None for a list typed in as `entries`.
    pub url: Option<String>,
    pub entries: Vec<String>,
    pub enabled: bool,
    /// `block`, `demote`, `exempt`, `trust`, or `kind:<kind>`. A list in `sources.lists` may show
    /// `tag:<tag>`.
    pub effect: String,
    /// A label its sites show, or empty.
    pub tag: String,
    /// Days between downloads. None for `sources.ttl_days`.
    pub ttl_days: Option<u64>,
    /// Whether popular sites and those a suffix rule decides are spared.
    pub guard: bool,
    /// Hosts, with their subdomains, this list does not apply to.
    pub except: Vec<String>,
    /// `Config` for a list in `sources.lists`, which can only be changed there.
    pub group: String,
    /// What the list's own repository states, `none` when nothing. Empty for the operator's lists.
    pub licence: String,
    pub custom: bool,
}

/// A list and how its last read and download went.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
pub struct SourceListInfo {
    #[serde(flatten)]
    pub list: SourceList,
    /// The hosts held now, 0 until a first download.
    pub hosts: u64,
    /// The rules it holds that are not whole hosts, which do nothing.
    pub skipped: u64,
    /// When the copy on disk was downloaded, in seconds since the epoch.
    pub fetched_at: Option<u64>,
    /// Why the last download failed; the older copy stays in use.
    pub error: Option<String>,
}

/// A list naming a site, and whether it counts.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
pub struct ListHit {
    pub id: String,
    pub name: String,
    pub effect: String,
    /// `applies`, or why not: `off`, `except` or `guard`.
    pub state: String,
}

/// What is known of a site, and every list that names it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
pub struct SourceExplanation {
    pub source: SourceInfo,
    pub lists: Vec<ListHit>,
}

/// What to change on a list: only the fields given. A catalog list keeps its `url` and `entries`.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ListChange {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub entries: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub enabled: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effect: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tag: Option<String>,
    /// 0 goes back to `sources.ttl_days`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ttl_days: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub guard: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub except: Option<Vec<String>>,
}

/// The kinds that make a site `high` on their own.
const HIGH_KINDS: [&str; 4] = ["intergovernmental", "government", "academic", "standards"];
const AVOID_TAGS: [&str; 6] =
    ["ai_generated", "content_farm", "predatory", "rsp_deprecated", "deny", "blocked"];

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
    } else if has(&["rsp_unreliable", "likely_farm", "demoted"]) {
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
    /// What decided it: config, mark, list, rule, wikidata or survey. Empty when nothing is known.
    /// A site set in the config cannot be marked.
    pub origin: String,
    /// Why the survey called it a likely farm. Left out otherwise.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub evidence: String,
    /// The lists that decided or tagged it, by name. Left out when none did.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub listed_by: Vec<String>,
}

/// A site's kind, tags and tier, without what only a person reading about it needs.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
pub struct SiteTags {
    pub kind: String,
    pub tags: Vec<String>,
    pub tier: Tier,
}

impl From<SourceInfo> for SiteTags {
    fn from(info: SourceInfo) -> Self {
        Self { kind: info.kind, tags: info.tags, tier: info.tier }
    }
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
            ("news", &["blocked"], Tier::Avoid),
            ("government", &["demoted"], Tier::Low),
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

    #[test]
    fn a_list_effect_and_tag_are_checked() {
        for good in ["block", "demote", "exempt", "trust", "kind:news"] {
            assert!(is_list_effect(good), "{good}");
        }
        for bad in ["", "allow", "kind:", "kind:unknown", "kind:shiny"] {
            assert!(!is_list_effect(bad), "{bad}");
        }
        for good in ["", "seo_spam", "ai2"] {
            assert!(is_list_tag(good), "{good}");
        }
        for bad in ["deny", "allow", "blocked", "content_farm", "rsp_reliable", "Has Space"] {
            assert!(!is_list_tag(bad), "{bad}");
        }
    }
}
