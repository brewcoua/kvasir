//! Wikipedia's perennial sources list, as `rsp_*` tags by host.

use std::collections::HashMap;
use std::sync::LazyLock;

use regex::Regex;
use serde::Deserialize;

use super::normalize;

// Every page the perennial sources list transcludes, in one answer: the rows are in its subpages.
pub(super) const RSP_URL: &str = "https://en.wikipedia.org/w/api.php?action=query&generator=templates\
    &titles=Wikipedia:Reliable_sources/Perennial_sources&gtlnamespace=4&gtllimit=max\
    &prop=revisions&rvprop=content&rvslots=main&formatversion=2&format=json";
// A row's class on the perennial sources page, and its tag. The least reliable comes last.
pub(super) const RSP_ORDER: [&str; 4] =
    ["rsp_reliable", "rsp_mixed", "rsp_unreliable", "rsp_deprecated"];
// Rows whose status does not apply here. Wikipedia is unreliable there for being circular
// (WP:CIRCULAR), an article citing an article; a run citing it is not circular.
pub(super) const RSP_SKIP: [&str; 1] = ["wikipedia.org"];

pub(super) static RSP_ROW: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?m)^\|-").expect("a pattern"));
pub(super) static RSP_STATUS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"\A\|- class="s-(\w+)""#).expect("a valid pattern"));
pub(super) static RSP_USES: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\{\{WP:RSPUSES\|([^{}]*)\}\}").expect("a valid pattern"));

#[derive(Deserialize)]
pub(super) struct RspAnswer {
    query: RspQuery,
}

#[derive(Deserialize)]
pub(super) struct RspQuery {
    pages: Vec<RspPage>,
}

#[derive(Deserialize)]
pub(super) struct RspPage {
    #[serde(default)]
    revisions: Vec<RspRevision>,
}

#[derive(Deserialize)]
pub(super) struct RspRevision {
    slots: RspSlots,
}

#[derive(Deserialize)]
pub(super) struct RspSlots {
    main: RspMain,
}

#[derive(Deserialize)]
pub(super) struct RspMain {
    content: String,
}

/// The `rsp_*` tag of each site on the perennial sources page, from the JSON the API answers with.
/// A row is `|- class="s-<status>"` and its sites are in `{{WP:RSPUSES|a.com|b.com}}`. A site in
/// several rows (a topic with a status of its own) takes the least reliable. Rows with no site, a
/// status that is none of the five, a part of a site (`a.com/news`) or a site in `RSP_SKIP` are
/// skipped.
pub fn parse_rsp(text: &str) -> Result<HashMap<String, String>, serde_json::Error> {
    let answer: RspAnswer = serde_json::from_str(text)?;
    let mut found: HashMap<String, String> = HashMap::new();
    for page in answer.query.pages {
        let Some(revision) = page.revisions.first() else {
            continue;
        };
        let wikitext = &revision.slots.main.content;
        let starts: Vec<usize> = RSP_ROW.find_iter(wikitext).map(|row| row.start()).collect();
        for (i, start) in starts.iter().enumerate() {
            let row = &wikitext[*start..starts.get(i + 1).copied().unwrap_or(wikitext.len())];
            let Some(status) = RSP_STATUS.captures(row) else {
                continue;
            };
            let Some(tag) = rsp_tag(&status[1]) else {
                continue;
            };
            for uses in RSP_USES.captures_iter(row) {
                for raw in uses[1].split('|') {
                    let (name, path) = raw.split_once('/').unwrap_or((raw, ""));
                    let Some(host) = normalize(name) else {
                        continue;
                    };
                    if RSP_SKIP.contains(&host.as_str()) || !matches!(path, "" | "*") {
                        continue;
                    }
                    let rank = |tag: &str| RSP_ORDER.iter().position(|known| *known == tag);
                    let held = found.get(&host).and_then(|held| rank(held));
                    if held.is_none() || rank(tag) > held {
                        found.insert(host, tag.to_owned());
                    }
                }
            }
        }
    }
    Ok(found)
}

pub(super) fn rsp_tag(status: &str) -> Option<&'static str> {
    match status {
        "gr" => Some("rsp_reliable"),
        "nc" => Some("rsp_mixed"),
        "gu" => Some("rsp_unreliable"),
        "d" | "b" => Some("rsp_deprecated"),
        _ => None,
    }
}
