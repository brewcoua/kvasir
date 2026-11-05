//! Searching again for what verification removed, and writing again what the search supports.

use std::collections::HashMap;

use indexmap::IndexMap;
use kvasir_research::verify::{self, Verified, unmarked};
use kvasir_research::{ResearchError, write};
use kvasir_run::stages::VERIFY;
use kvasir_types::results::QuestionStatus;
use kvasir_types::runs::Progress;

use crate::questions::Question;
use crate::search_loop::{Judge, Research, research};

/// Sentences verification removed that are searched for again, at most, and the searches that may
/// take. One pass: a rewritten section's own removals stand.
const REPAIR_SENTENCES: usize = 4;
const REPAIR_SEARCHES: usize = 4;

/// Searches for what verification removed, and writes again the sections the search supports.
///
/// A removed sentence is often true, cited to a claim that does not say it. Each becomes a
/// question; a section whose question the search answers is written again with the claims that
/// answer it, and checked again; what that check did to its sentences replaces what the first
/// check did. The questions are numbered from `first_id`, after those of the research.
pub async fn repair(
    run: &mut Research<'_>,
    ledger: &mut Vec<Question>,
    titles: &[String],
    first_id: u64,
    checked: Verified,
) -> Result<Verified, ResearchError> {
    let picked: Vec<_> = checked
        .removed
        .iter()
        .filter(|r| titles.contains(&r.section))
        .take(REPAIR_SENTENCES)
        .cloned()
        .collect();
    if picked.is_empty() {
        return Ok(checked);
    }
    let mut questions: Vec<Question> = picked
        .iter()
        .zip(first_id..)
        .map(|(removed, id)| Question {
            missing: removed.reason.clone(),
            judged_at: run.claims.len(),
            ..Question::new(id, unmarked(&removed.sentence))
        })
        .collect();
    let (tools, ctx) = (run.tools, run.ctx);
    let detail = format!("searching again for {} removed sentences", picked.len());
    let searching =
        ctx.publish(Progress { kind: Some("repair".into()), ..Progress::new(VERIFY, detail) });
    let judge = Judge::Check;
    research(
        run,
        ledger,
        Some(&mut questions),
        0,
        REPAIR_SEARCHES,
        &judge,
        VERIFY,
        Some(searching),
    )
    .await?;
    let mut found: IndexMap<String, Vec<u64>> = IndexMap::new();
    for (sentence, question) in picked.iter().zip(&questions) {
        if question.status == QuestionStatus::Answered {
            found.entry(sentence.section.clone()).or_default().extend(&question.claims);
        }
    }
    let detail = format!("found support for {} of the sections they came from", found.len());
    ctx.publish(Progress::new(VERIFY, detail));

    let by_id: HashMap<u64, _> = run.claims.iter().map(|c| (c.id, c.clone())).collect();
    let Verified { mut sections, mut removed, mut revised, mut unverified } = checked;
    for (title, ids) in found {
        let index = sections.iter().position(|(t, _)| *t == title).expect("a written section");
        ctx.publish(Progress::new(VERIFY, format!("writing {title} again")));
        let earlier: Vec<(String, String)> =
            sections[..index].iter().filter(|(t, _)| titles.contains(t)).cloned().collect();
        let text = write::section(
            tools,
            ctx,
            &run.brief,
            titles,
            &title,
            &earlier,
            &run.claims,
            &ids,
            &[],
        )
        .await?;
        let again = verify::report(tools, ctx, &[(title.clone(), text)], &by_id).await?;
        sections[index] = again.sections.into_iter().next().expect("the section checked");
        removed.retain(|r| r.section != title);
        removed.extend(again.removed);
        revised.retain(|r| r.section != title);
        revised.extend(again.revised);
        unverified.retain(|u| u.section != title);
        unverified.extend(again.unverified);
    }
    Ok(Verified { sections, removed, revised, unverified })
}
