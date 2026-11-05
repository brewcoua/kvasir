//! The question ledger: a brief split into questions, searched for one at a time.
//!
//! Code decides how much to search and when to stop; the model only decides what to search for,
//! what a page says, and how to write it up. Every model call sees one small input, so a 16k
//! context is enough. See `docs/research.md` for the design.

pub mod brief;
pub mod compare;
pub mod frontier;
pub mod questions;
pub mod repair;
pub mod search_loop;
pub mod signatures;

use std::collections::{HashMap, HashSet};
use std::time::Instant;

use kvasir_config::ResearchSettings;
use kvasir_research::corpus::Corpus;
use kvasir_research::corroborate::cross_check;
use kvasir_research::render::{SUMMARY, matrix, render};
use kvasir_research::text::names;
use kvasir_research::{ResearchError, Tools, verify, write};
use kvasir_run::RunCtx;
use kvasir_run::stages::{ASSESS, BACKGROUND, BRIEF, RESEARCH, VERIFY, WRITE};
use kvasir_types::requests::{Frontier as Order, Input, LedgerOptions};
use kvasir_types::results::{Criterion, Output, ResearchResult};
use kvasir_types::runs::Progress;

use crate::brief::{candidates, clarifications};
use crate::compare::{assess, fill, rank, verify_cells};
use crate::frontier::Frontier;
use crate::questions::{MAX_QUESTIONS, Question, add_questions, as_results, notes, open_questions};
use crate::repair::repair;
use crate::search_loop::{Judge, Research, research as search};
use crate::signatures::{NamedQuestion, brief as frame, decompose, outline};

/// Questions the first split of the brief adds at most.
const FIRST_QUESTIONS: usize = 6;

pub const NO_EVIDENCE: &str = "## No findings\n\nNo page read during the research supported a \
claim about this question. Try rephrasing it, or answering the clarifying questions.";

fn shape(output: Output) -> &'static str {
    match output {
        Output::Report => {
            "The first section gives the key findings. Each later section covers one part of the \
             answer."
        }
        Output::Ranking => {
            "A ranking table comes first and is already built. Start with how the options were \
             compared, then one section per option, best first."
        }
        Output::Table => {
            "A comparison table comes first and is already built. Write a short summary of what \
             it shows, then at most two sections on what it leaves open."
        }
    }
}

fn table_title(output: Output) -> &'static str {
    if output == Output::Ranking { "Ranking" } else { "Comparison" }
}

/// The stages a run with these options plans to go through. Background and assess happen only
/// for a ranking or a table, which the brief may choose.
pub fn stages(options: &LedgerOptions) -> Vec<&'static str> {
    if options.output == Some(Output::Report) {
        return vec![BRIEF, RESEARCH, WRITE, VERIFY];
    }
    vec![BRIEF, BACKGROUND, RESEARCH, ASSESS, WRITE, VERIFY]
}

/// Researches a question to a cited report, `tools` holding the models the options chose and
/// `defaults` the budgets they leave out. `today` is the date the brief is told.
pub async fn research(
    tools: &Tools,
    defaults: &ResearchSettings,
    ctx: &RunCtx,
    input: &Input,
    options: &LedgerOptions,
    today: &str,
) -> Result<ResearchResult, ResearchError> {
    let started = Instant::now();
    let setting = |value: Option<u64>, default: usize| value.map_or(default, |v| v as usize);
    let budget = setting(options.max_searches, defaults.max_searches);
    let per_query = setting(options.results_per_query, defaults.results_per_query);
    let max_attempts = setting(options.max_attempts, defaults.max_attempts);
    let follow_ups = options.follow_ups.unwrap_or(defaults.follow_ups);
    let max_depth = setting(options.max_depth, defaults.max_depth) as u64;

    ctx.publish(Progress::new(BRIEF, "writing the research brief"));
    let asked_for = clarifications(&input.clarifications);
    let framed = tools
        .predict::<frame::Sig>(
            ctx,
            "brief",
            &frame::Input {
                today: today.to_owned(),
                question: input.question.clone(),
                clarifications: asked_for.clone(),
            },
        )
        .await?;
    let mut brief = framed.brief;
    let mut output = options.output.unwrap_or(framed.output.into());
    let corpus = Corpus::new(
        tools.pages.cache(),
        tools.encoder.name(),
        options.recall_past_runs,
        tools.sources.clone(),
    );

    // Named in the question, they are final; the options override even those. The brief lists
    // options from memory as readily as from the question, so only those the question really
    // names count as named.
    let asked = format!("{}\n{asked_for}", input.question);
    let named = |given: &Option<Vec<String>>, framed: Vec<String>| match given {
        Some(given) if !given.is_empty() => given.clone(),
        _ => framed.into_iter().filter(|name| names(name, &asked)).collect(),
    };
    let mut criteria: Vec<Criterion> = named(&options.criteria, framed.criteria)
        .into_iter()
        .map(|name| Criterion { name, weight: 1 })
        .collect();
    let mut entities = named(&options.entities, framed.entities);
    if output != Output::Report {
        (criteria, entities) = candidates(
            tools,
            ctx,
            today,
            &brief,
            criteria,
            entities,
            setting(options.max_criteria, defaults.max_criteria),
            setting(options.max_entities, defaults.max_entities),
            per_query,
        )
        .await?;
        if criteria.is_empty() || entities.is_empty() {
            let detail = "found nothing to compare, so writing a report instead";
            ctx.publish(Progress::new(BACKGROUND, detail));
            output = Output::Report;
        } else {
            // Every later step reads the brief, so naming the options here steers the searches
            // and the intake towards them.
            let named: Vec<&str> = criteria.iter().map(|c| c.name.as_str()).collect();
            brief.push_str(&format!(
                "\nOptions compared: {}\nCriteria: {}",
                entities.join(", "),
                named.join(", ")
            ));
        }
    }

    ctx.publish(Progress {
        kind: Some("split".into()),
        ..Progress::new(RESEARCH, "splitting the brief into questions")
    });
    let input_split = decompose::Input { brief: brief.clone() };
    let mut first =
        tools.predict::<decompose::Sig>(ctx, "decompose", &input_split).await?.questions;
    first.truncate(FIRST_QUESTIONS);
    if first.is_empty() {
        first =
            vec![NamedQuestion { question: input.question.clone(), ..NamedQuestion::default() }];
    }
    let mut ledger: Vec<Question> = Vec::new();
    add_questions(&tools.encoder, ctx, &mut ledger, &first, None, MAX_QUESTIONS).await?;
    ctx.run.record_questions(as_results(&ledger));

    // Only the first pass: filling cells and repairing sentences search for set questions.
    let frontier = match options.frontier {
        Order::Worth => Some(Frontier::new(&tools.encoder, &brief, ctx).await?),
        Order::Breadth => None,
    };
    let mut run = Research {
        tools,
        ctx,
        today: today.to_owned(),
        brief,
        claims: Vec::new(),
        seen: HashSet::new(),
        corpus,
        per_query,
        max_attempts,
        searched: 0,
        frontier,
        rows: Vec::new(),
    };
    let judge = Judge::Ledger { follow_ups, max_depth };
    search(&mut run, &mut ledger, None, 0, budget, &judge, RESEARCH, None).await?;
    run.frontier = None;

    if run.claims.is_empty() {
        // Writing sections from no evidence only produces paragraphs saying there is none.
        ctx.publish(Progress::new(WRITE, "no evidence found, so nothing to write"));
        return Ok(ResearchResult {
            report: NO_EVIDENCE.into(),
            assumptions: framed.assumptions,
            questions: as_results(&ledger),
            output,
            duration_seconds: seconds(started),
            ..ResearchResult::default()
        });
    }

    let (mut cells_removed, mut cells_unverified) = (Vec::new(), Vec::new());
    if output != Output::Report {
        run.rows = assess(tools, ctx, &run.brief, &run.claims, &entities, &criteria).await?;
        fill(&mut run, &mut ledger).await?;
        // Filling the cells searched, so this is built after it.
        let by_id = by_id(&run.claims);
        (cells_removed, cells_unverified) =
            verify_cells(tools, ctx, &mut run.rows, &by_id, table_title(output)).await?;
        if output == Output::Ranking {
            let rows = std::mem::take(&mut run.rows);
            run.rows = rank(tools, ctx, &run.brief, &criteria, rows).await?;
            let ranked: Vec<&str> = run.rows.iter().map(|row| row.entity.as_str()).collect();
            run.brief.push_str(&format!("\nOverall ranking, best first: {}", ranked.join(", ")));
        }
    }

    ctx.publish(Progress::new(WRITE, "outlining the report"));
    let input_outline = outline::Input {
        brief: run.brief.clone(),
        shape: shape(output).into(),
        notes: notes(&ledger),
    };
    let titles = tools.predict::<outline::Sig>(ctx, "outline", &input_outline).await?.sections;
    let total = titles.len() as u64;
    let mut sections: Vec<(String, String)> = Vec::new();
    for (done, title) in titles.iter().enumerate() {
        ctx.publish(Progress {
            done: Some(done as u64),
            total: Some(total),
            kind: Some("section".into()),
            ..Progress::new(WRITE, format!("writing {title}"))
        });
        let text = write::section(
            tools,
            ctx,
            &run.brief,
            &titles,
            title,
            &sections,
            &run.claims,
            &[],
            &[],
        )
        .await?;
        sections.push((title.clone(), text));
    }
    if !run.rows.is_empty() {
        sections.insert(0, (table_title(output).into(), matrix(&run.rows, &criteria)));
    }
    let ended = |detail: &str| {
        ctx.publish(Progress {
            done: Some(total),
            total: Some(total),
            ..Progress::new(WRITE, detail)
        })
    };
    ended("summing up the report");
    let summary = write::summary(tools, ctx, &run.brief, &sections).await?;
    ended("assembled the report");
    let left = open_questions(&ledger);
    if !left.is_empty() {
        sections.push(("Open questions".into(), left));
    }

    // The summary is checked with the sections, so the checker is loaded once. Repair leaves it be.
    let mut all = vec![(SUMMARY.to_owned(), summary)];
    all.extend(sections);
    let mut checked = verify::report(tools, ctx, &all, &by_id(&run.claims)).await?;
    let (_, summary) = checked.sections.remove(0);
    checked.removed.extend(cells_removed);
    checked.unverified.extend(cells_unverified);

    let first_id = ledger.iter().map(|q| q.id).max().unwrap_or(0) + 1;
    let checked = repair(&mut run, &mut ledger, &titles, first_id, checked).await?;

    cross_check(tools, ctx, &mut run.claims, options.corroborate, options.contradict).await?;

    let by_id = by_id(&run.claims);
    let (report, citations) = render(&checked.sections, &by_id, &summary);
    let mut all = vec![(SUMMARY.to_owned(), summary)];
    all.extend(checked.sections);
    let low_trust = verify::low_trust(&all, &by_id, &run.corpus.low_trust(&run.claims).await);
    let matrix = run.rows;
    Ok(ResearchResult {
        report,
        assumptions: framed.assumptions,
        citations,
        claims: run.claims,
        questions: as_results(&ledger),
        removed: checked.removed,
        revised: checked.revised,
        unverified: checked.unverified,
        low_trust,
        output,
        criteria: if matrix.is_empty() { Vec::new() } else { criteria },
        matrix,
        duration_seconds: seconds(started),
    })
}

fn by_id(
    claims: &[kvasir_research::claims::Claim],
) -> HashMap<u64, kvasir_research::claims::Claim> {
    claims.iter().map(|claim| (claim.id, claim.clone())).collect()
}

/// Seconds since `started`, to one decimal.
fn seconds(started: Instant) -> f64 {
    (started.elapsed().as_secs_f64() * 10.0).round() / 10.0
}
