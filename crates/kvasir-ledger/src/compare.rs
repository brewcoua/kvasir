//! Filling a ranking or a table: one finding per option and criterion, then ranks.

use std::collections::HashMap;
use std::sync::LazyLock;

use indexmap::IndexMap;
use kvasir_llm::LlmError;
use kvasir_research::claims::{Claim, listed, nearest};
use kvasir_research::text::names;
use kvasir_research::verify::{Status, UNVERIFIED, cascade};
use kvasir_research::{ResearchError, Tools, readable};
use kvasir_run::RunCtx;
use kvasir_run::stages::{ASSESS, RESEARCH};
use kvasir_types::results::{Cell, Criterion, RemovedSentence, Row, UnverifiedSentence};
use kvasir_types::runs::Progress;
use regex::Regex;

use crate::questions::Question;
use crate::search_loop::{Judge, Research, research};
use crate::signatures::{assess, rank_criterion};

/// Claims shown when assessing one option on one criterion, the closest to the pair.
const CLAIMS_PER_CELL: usize = 8;
/// Searches for the cells still empty after the research, on top of its budget.
const FILL_SEARCHES: usize = 8;

static NUMBERED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\s*\d+[.)]\s*").expect("a valid pattern"));

fn publish(ctx: &RunCtx, detail: String, done: usize, total: usize, series: Option<&str>) {
    ctx.publish(Progress {
        done: Some(done as u64),
        total: Some(total as u64),
        series: series.map(str::to_owned),
        ..Progress::new(ASSESS, detail)
    });
}

/// Each option's finding on each criterion, from the claims the research gathered.
pub async fn assess(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    claims: &[Claim],
    entities: &[String],
    criteria: &[Criterion],
) -> Result<Vec<Row>, LlmError> {
    let total = entities.len() * criteria.len();
    let texts: Vec<String> = claims.iter().map(|c| c.text.clone()).collect();
    let vectors = tools.encoder.encode(&texts, false, ctx).await?;
    let mut rows = Vec::new();
    for entity in entities {
        let mut cells = Vec::new();
        for criterion in criteria {
            let detail = format!("assessing {entity} on {}", criterion.name);
            publish(ctx, detail, rows.len() * criteria.len() + cells.len(), total, Some("assess"));
            cells.push(cell(tools, ctx, &vectors, brief, claims, entity, &criterion.name).await?);
        }
        rows.push(Row { entity: entity.clone(), rank: None, cells });
    }
    Ok(rows)
}

/// Searches for the empty cells of the run's rows as questions of their own, added to `ledger`,
/// and assesses them again.
///
/// A cell's question is answered once assessing it finds a claim, so the assessment is its
/// judgement and no other call is needed.
pub async fn fill(run: &mut Research<'_>, ledger: &mut Vec<Question>) -> Result<(), ResearchError> {
    let mut cells: IndexMap<(String, String), (usize, usize)> = IndexMap::new();
    for (r, row) in run.rows.iter().enumerate() {
        for (c, cell) in row.cells.iter().enumerate() {
            cells.insert((row.entity.clone(), cell.criterion.clone()), (r, c));
        }
    }
    let empty: Vec<(String, String)> = cells
        .iter()
        .filter(|&(_, &(r, c))| run.rows[r].cells[c].claims.is_empty())
        .map(|(key, _)| key.clone())
        .collect();
    if empty.is_empty() {
        return Ok(());
    }
    let from = ledger.len();
    for (n, (entity, criterion)) in empty.iter().enumerate() {
        let mut question = Question::new((from + n + 1) as u64, format!("{criterion} of {entity}"));
        question.cell = Some((entity.clone(), criterion.clone()));
        question.judged_at = run.claims.len();
        ledger.push(question);
    }
    run.ctx.publish(Progress::new(ASSESS, format!("searching for {} empty cells", empty.len())));
    let judge = Judge::Cells(cells.clone());
    research(run, ledger, None, from, FILL_SEARCHES, &judge, RESEARCH, None).await?;
    let filled = empty
        .iter()
        .filter(|key| {
            let (r, c) = cells[*key];
            !run.rows[r].cells[c].claims.is_empty()
        })
        .count();
    let detail = format!("filled {filled} of {} empty cells", empty.len());
    run.ctx.publish(Progress::new(ASSESS, detail));
    Ok(())
}

/// What the claims closest to `entity` and `criterion` say of it, from their `vectors`.
pub async fn cell(
    tools: &Tools,
    ctx: &RunCtx,
    vectors: &[Vec<f32>],
    brief: &str,
    claims: &[Claim],
    entity: &str,
    criterion: &str,
) -> Result<Cell, LlmError> {
    let query = format!("{entity}: {criterion}");
    let shown: Vec<&Claim> = nearest(&tools.encoder, &query, vectors, CLAIMS_PER_CELL, ctx)
        .await?
        .into_iter()
        .map(|i| &claims[i])
        .collect();
    let empty = Cell {
        criterion: criterion.to_owned(),
        finding: String::new(),
        claims: Vec::new(),
        rank: None,
    };
    let input = assess::Input {
        brief: brief.to_owned(),
        entity: entity.to_owned(),
        criterion: criterion.to_owned(),
        evidence: listed(shown.iter().copied()),
    };
    let Some(assessed) = readable(tools.predict::<assess::Sig>(ctx, "assess", &input).await)?
    else {
        return Ok(empty);
    };
    // Only ids it was shown, of claims that name the entity: a small model otherwise cites another
    // option's figures for this one.
    let ids: Vec<u64> = shown
        .iter()
        .filter(|c| assessed.claims.iter().any(|&id| u64::try_from(id) == Ok(c.id)))
        .filter(|c| names(entity, &format!("{} {} {}", c.text, c.quote, c.title)))
        .map(|c| c.id)
        .collect();
    let finding = assessed.finding.trim().to_owned();
    if ids.is_empty() || finding.is_empty() {
        return Ok(empty);
    }
    Ok(Cell { finding, claims: ids, ..empty })
}

/// Checks each cell with a finding against the claims it cites, and empties or marks the doubtful.
///
/// A table is not a sentence to cut, so verifying the report leaves it alone; its cells go through
/// the same cascade here, all together so each model loads once. A cell is at most six words, so a
/// partly supported one is emptied rather than rewritten. An emptied cell ties for last when
/// ranked, like any cell no claim spoke to.
pub async fn verify_cells(
    tools: &Tools,
    ctx: &RunCtx,
    rows: &mut [Row],
    by_id: &HashMap<u64, Claim>,
    title: &str,
) -> Result<(Vec<RemovedSentence>, Vec<UnverifiedSentence>), LlmError> {
    let mut cells: Vec<&mut Cell> = Vec::new();
    let mut items: Vec<(String, Vec<Claim>)> = Vec::new();
    for row in rows.iter_mut() {
        for cell in row.cells.iter_mut().filter(|cell| !cell.claims.is_empty()) {
            let markers: String = cell.claims.iter().map(|id| format!("[{id}]")).collect();
            let text = format!("{}, {}: {} {markers}", row.entity, cell.criterion, cell.finding);
            items.push((text, cell.claims.iter().map(|id| by_id[id].clone()).collect()));
            cells.push(cell);
        }
    }
    if cells.is_empty() {
        return Ok((Vec::new(), Vec::new()));
    }
    ctx.publish(Progress::new(
        ASSESS,
        format!("checking {} cells against their quotes", cells.len()),
    ));
    let verdicts = cascade(tools, ctx, &items, ASSESS).await?;
    let mut removed = Vec::new();
    let mut unverified = Vec::new();
    for (((text, _), cell), verdict) in items.into_iter().zip(cells).zip(verdicts) {
        match verdict.status {
            Status::Removed | Status::Rewritten => {
                let reason = if verdict.status == Status::Rewritten {
                    format!(
                        "a partly supported cell is removed rather than rewritten ({})",
                        verdict.reason
                    )
                } else {
                    verdict.reason
                };
                removed.push(RemovedSentence {
                    section: title.to_owned(),
                    sentence: text,
                    reason,
                    stage: verdict.stage,
                });
                cell.finding.clear();
                cell.claims.clear();
            }
            Status::Unverified => {
                let reason = verdict.reason;
                unverified.push(UnverifiedSentence {
                    section: title.to_owned(),
                    sentence: text,
                    reason,
                });
                cell.finding.push_str(&format!(" {UNVERIFIED}"));
            }
            Status::Kept => {}
        }
    }
    Ok((removed, unverified))
}

/// `rows` best first, each ranked on every criterion twice, the second time in reverse order.
///
/// A model favours the options it reads first; reversing the order and averaging cancels that.
/// Only options with a finding are judged; those without one tie for the places after them, since
/// a judge asked to rank them anyway places them by guesswork. The overall order is the weighted
/// mean of the criterion ranks, computed here, not judged.
pub async fn rank(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    criteria: &[Criterion],
    mut rows: Vec<Row>,
) -> Result<Vec<Row>, LlmError> {
    let total = 2 * criteria.len();
    for (index, criterion) in criteria.iter().enumerate() {
        let found: Vec<usize> =
            (0..rows.len()).filter(|&r| !rows[r].cells[index].claims.is_empty()).collect();
        let mut places: HashMap<String, Vec<usize>> =
            found.iter().map(|&r| (rows[r].entity.clone(), Vec::new())).collect();
        if let [only] = found[..] {
            places.get_mut(&rows[only].entity).expect("found").push(1);
        }
        let reversed: Vec<usize> = found.iter().rev().copied().collect();
        let passes: Vec<&[usize]> =
            if found.len() > 1 { vec![&found, &reversed] } else { Vec::new() };
        for (turn, order) in passes.into_iter().enumerate() {
            let detail = format!("ranking on {}, pass {} of 2", criterion.name, turn + 1);
            publish(ctx, detail, 2 * index + turn, total, Some("rank"));
            // The mark is for the reader; the judge would read it as part of the finding.
            let suffix = format!(" {UNVERIFIED}");
            let options: Vec<String> = order
                .iter()
                .map(|&r| {
                    let finding = &rows[r].cells[index].finding;
                    format!(
                        "{}: {}",
                        rows[r].entity,
                        finding.strip_suffix(&suffix).unwrap_or(finding)
                    )
                })
                .collect();
            let input = rank_criterion::Input {
                brief: brief.to_owned(),
                criterion: criterion.name.clone(),
                options: options.join("\n"),
            };
            // Unread, the input order stands in; the other pass, reversed, cancels it into a tie.
            let ranking =
                readable(tools.predict::<rank_criterion::Sig>(ctx, "judge", &input).await)?
                    .map(|answer| answer.ranking)
                    .unwrap_or_default();
            let names: Vec<String> = order.iter().map(|&r| rows[r].entity.clone()).collect();
            for (place, entity) in ordered(&ranking, &names).into_iter().enumerate() {
                places.get_mut(&entity).expect("a ranked option").push(place + 1);
            }
        }
        let last = (found.len() + 1 + rows.len()) as f64 / 2.0;
        for row in &mut rows {
            row.cells[index].rank = Some(match places.get(&row.entity) {
                Some(placed) => placed.iter().sum::<usize>() as f64 / placed.len() as f64,
                None => last,
            });
        }
    }
    let weight: f64 = criteria.iter().map(|c| f64::from(c.weight)).sum();
    let score = |row: &Row| {
        criteria
            .iter()
            .zip(&row.cells)
            .map(|(c, cell)| f64::from(c.weight) * cell.rank.unwrap_or(0.0))
            .sum::<f64>()
            / weight
    };
    rows.sort_by(|a, b| score(a).total_cmp(&score(b)));
    for (place, row) in rows.iter_mut().enumerate() {
        row.rank = Some(place as u64 + 1);
    }
    let ranked: Vec<&str> = rows.iter().map(|row| row.entity.as_str()).collect();
    publish(ctx, format!("ranked: {}", ranked.join(", ")), total, total, None);
    Ok(rows)
}

/// `names` in the order `ranking` gives them, those it leaves out last, in their own order.
///
/// Models number the list and change the case, so a name is matched without either.
pub fn ordered(ranking: &[String], names: &[String]) -> Vec<String> {
    let known: HashMap<String, &String> = names.iter().map(|n| (n.to_lowercase(), n)).collect();
    let mut found: Vec<String> = Vec::new();
    for item in ranking {
        let key = NUMBERED.replace(item, "").trim().to_lowercase();
        if let Some(&name) = known.get(&key)
            && !found.contains(name)
        {
            found.push(name.clone());
        }
    }
    let left: Vec<String> = names.iter().filter(|n| !found.contains(n)).cloned().collect();
    found.extend(left);
    found
}
