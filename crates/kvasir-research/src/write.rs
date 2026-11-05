//! Writing one section of a report from the claims closest to it, and the report's summary.

use std::collections::HashSet;

use kvasir_extract::head;
use kvasir_llm::LlmError;
use kvasir_run::RunCtx;

use crate::claims::{Claim, closest, listed};
use crate::read::page_characters;
use crate::signatures::{summarize, write_section};
use crate::style::clean;
use crate::{Tools, readable};

/// Claims given to one section, the closest to its title.
pub const CLAIMS_PER_SECTION: usize = 15;
/// How much of each earlier section a later one sees, to avoid repeating it without filling the
/// context with it.
const EARLIER_SECTION_CHARACTERS: usize = 400;

/// One section, from the claims closest to its title and the `extra` claim ids.
///
/// Empty when the answer cannot be read, such as a thinking model spending its whole budget on
/// reasoning, so one section does not end the run.
#[allow(clippy::too_many_arguments)]
pub async fn section(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    titles: &[String],
    title: &str,
    earlier: &[(String, String)],
    claims: &[Claim],
    extra: &[u64],
    subsections: &[String],
) -> Result<String, LlmError> {
    let texts: Vec<String> = claims.iter().map(|claim| claim.text.clone()).collect();
    let query = format!("{title}\n{brief}");
    let shown = closest(&tools.encoder, &query, &texts, CLAIMS_PER_SECTION, ctx).await?;
    let mut ids: HashSet<u64> = shown.into_iter().map(|i| claims[i].id).collect();
    ids.extend(extra);
    let earlier: Vec<String> = earlier
        .iter()
        .map(|(t, body)| format!("{t}: {}", head(body, EARLIER_SECTION_CHARACTERS)))
        .collect();
    let input = write_section::Input {
        brief: brief.to_owned(),
        outline: titles.join("\n"),
        section: title.to_owned(),
        subsections: subsections.join("\n"),
        earlier_sections: earlier.join("\n\n"),
        evidence: listed(claims.iter().filter(|claim| ids.contains(&claim.id))),
    };
    let answer = readable(tools.predict::<write_section::Sig>(ctx, "write", &input).await)?;
    Ok(answer.map(|a| clean(&a.text)).unwrap_or_default())
}

/// The report's main findings as a cited list, or nothing when they cannot be read.
///
/// Shown the start of each section, as much as the context holds shared between them, since a
/// section leads with its finding.
pub async fn summary(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    sections: &[(String, String)],
) -> Result<String, LlmError> {
    let each = page_characters(tools.context_tokens) / sections.len().max(1);
    let report: Vec<String> =
        sections.iter().map(|(title, text)| format!("## {title}\n{}", head(text, each))).collect();
    let input = summarize::Input { brief: brief.to_owned(), report: report.join("\n\n") };
    let Some(answer) = readable(tools.predict::<summarize::Sig>(ctx, "tldr", &input).await)? else {
        return Ok(String::new());
    };
    let points: Vec<String> = answer
        .points
        .iter()
        .filter(|point| !point.trim().is_empty())
        .map(|point| {
            format!("- {}", clean(&point.split_whitespace().collect::<Vec<_>>().join(" ")))
        })
        .collect();
    Ok(points.join("\n"))
}
