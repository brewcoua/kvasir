//! The graph method: questions explored best first, each raising the next from its evidence.
//!
//! The brief is split into first questions, as in the ledger. Exploration then searches the
//! question proof numbers pick, takes in its claims, and adds the questions they raise as its
//! children, until the research time is spent or no question can still be settled. The report has
//! sections an outline call groups the questions into after the research, each written from the
//! claims its questions rest on. Always a report: the graph has no ranking or table. See
//! `docs/research.md` for the design.

pub mod explore;
pub mod graph;
pub mod proof;
pub mod signatures;

use std::collections::{BTreeSet, HashMap, HashSet};
use std::time::Duration;

use indexmap::IndexMap;
use kvasir_config::ResearchSettings;
use kvasir_ledger::brief::clarifications;
use kvasir_ledger::frontier::Frontier;
use kvasir_ledger::questions::{Question, add_questions, as_results, status_name, unsettled};
use kvasir_ledger::search_loop::{Research, purge};
use kvasir_ledger::signatures::{NamedQuestion, brief as frame, decompose};
use kvasir_ledger::{FIRST_QUESTIONS, NO_EVIDENCE};
use kvasir_llm::LlmError;
use kvasir_research::claims::{Claim, nearest};
use kvasir_research::corpus::Corpus;
use kvasir_research::corroborate::cross_check;
use kvasir_research::render::{SUMMARY, render};
use kvasir_research::{ResearchError, Tools, readable, verify, write};
use kvasir_run::RunCtx;
use kvasir_run::stages::{BRIEF, RESEARCH, VERIFY, WRITE};
use kvasir_types::requests::{GraphOptions, Input};
use kvasir_types::results::{QuestionStatus, ResearchResult};
use kvasir_types::runs::Progress;
use tokio::time::Instant;

use crate::explore::{LATE_SHARE, explore};
use crate::graph::branches;
use crate::signatures::outline as draft;

/// Levels of raised questions below the first questions, unless the run sets it. Deeper than the
/// ledger's: the time budget, not the depth, is what bounds the graph.
pub const MAX_DEPTH: u64 = 4;
/// Pages read per search. The ledger's setting is 3; the graph picks them through the fusion in
/// `research::search`, so it can afford to read more.
pub const RESULTS_PER_QUERY: usize = 5;
/// Sections a report has at most, as STORM's outlines do; more read as a list of findings.
const MAX_SECTIONS: usize = 7;
/// Questions the report lists as open, at most.
const OPEN_QUESTIONS: usize = 10;

pub const STAGES: [&str; 4] = [BRIEF, RESEARCH, WRITE, VERIFY];

/// Researches a question to a cited report, `tools` holding the models the options chose and
/// `defaults` the budgets they leave out. `today` is the date the brief is told.
pub async fn research(
    tools: &Tools,
    defaults: &ResearchSettings,
    ctx: &RunCtx,
    input: &Input,
    options: &GraphOptions,
    today: &str,
) -> Result<ResearchResult, ResearchError> {
    let started = Instant::now();
    let minutes = Duration::from_secs_f64(options.research_minutes * 60.0);
    let deadline = started + minutes;
    let late_at = started + minutes.mul_f64(1.0 - LATE_SHARE);

    ctx.publish(Progress::new(BRIEF, "writing the research brief"));
    let framed = tools
        .predict::<frame::Sig>(
            ctx,
            "brief",
            &frame::Input {
                today: today.to_owned(),
                question: input.question.clone(),
                clarifications: clarifications(&input.clarifications),
            },
        )
        .await?;
    let brief = framed.brief;

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
    let mut questions: Vec<Question> = Vec::new();
    add_questions(&tools.encoder, ctx, &mut questions, &first, None, explore::MAX_QUESTIONS)
        .await?;
    ctx.run.record_questions(as_results(&questions));
    let corpus = Corpus::new(
        tools.pages.cache(),
        tools.encoder.name(),
        options.recall_past_runs,
        tools.sources.clone(),
    );
    let mut frontier = Frontier::new(&tools.encoder, &brief, ctx).await?;
    let setting = |value: Option<u64>, default: usize| value.map_or(default, |v| v as usize);
    let mut run = Research {
        tools,
        ctx,
        today: today.to_owned(),
        brief,
        claims: Vec::new(),
        seen: HashSet::new(),
        corpus,
        per_query: setting(options.results_per_query, RESULTS_PER_QUERY),
        max_attempts: setting(options.max_attempts, defaults.max_attempts),
        searched: 0,
        frontier: None,
        rows: Vec::new(),
    };
    let budget = options.max_searches.map(|budget| budget as usize);
    let max_depth = options.max_depth.unwrap_or(MAX_DEPTH);
    explore(&mut run, &mut frontier, &mut questions, deadline, late_at, budget, max_depth).await?;
    purge(&mut run, questions.iter_mut()).await;

    let seconds = || (started.elapsed().as_secs_f64() * 10.0).round() / 10.0;
    if run.claims.is_empty() {
        ctx.publish(Progress::new(WRITE, "no evidence found, so nothing to write"));
        return Ok(ResearchResult {
            report: NO_EVIDENCE.into(),
            assumptions: framed.assumptions,
            questions: as_results(&questions),
            duration_seconds: seconds(),
            ..ResearchResult::default()
        });
    }

    ctx.publish(Progress::new(WRITE, "outlining the report"));
    let found = outline(tools, ctx, &run.brief, &questions).await?;
    let titles: Vec<String> = found.iter().map(|(title, _, _)| title.clone()).collect();
    let total = found.len() as u64;
    let mut sections: Vec<(String, String)> = Vec::new();
    for (done, (title, subsections, ids)) in found.iter().enumerate() {
        ctx.publish(Progress {
            done: Some(done as u64),
            total: Some(total),
            kind: Some("section".into()),
            ..Progress::new(WRITE, format!("writing {title}"))
        });
        let ids: Vec<u64> = ids.iter().copied().collect();
        let text = write::section(
            tools,
            ctx,
            &run.brief,
            &titles,
            title,
            &sections,
            &run.claims,
            &ids,
            subsections,
        )
        .await?;
        sections.push((title.clone(), text));
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
    let left = open_questions(&questions);
    if !left.is_empty() {
        sections.push(("Open questions".into(), left));
    }

    // The summary is checked with the sections, so the checker is loaded once.
    let mut all = vec![(SUMMARY.to_owned(), summary)];
    all.extend(sections);
    let mut checked = verify::report(tools, ctx, &all, &by_id(&run.claims)).await?;
    let (_, summary) = checked.sections.remove(0);
    cross_check(tools, ctx, &mut run.claims, options.corroborate, options.contradict).await?;
    let by_id = by_id(&run.claims);
    let (report, citations) = render(&checked.sections, &by_id, &summary);
    let mut all = vec![(SUMMARY.to_owned(), summary)];
    all.extend(checked.sections);
    let low_trust = verify::low_trust(&all, &by_id, &run.corpus.low_trust(&run.claims).await);
    Ok(ResearchResult {
        report,
        assumptions: framed.assumptions,
        citations,
        claims: run.claims,
        questions: as_results(&questions),
        removed: checked.removed,
        revised: checked.revised,
        unverified: checked.unverified,
        low_trust,
        duration_seconds: seconds(),
        ..ResearchResult::default()
    })
}

fn by_id(claims: &[Claim]) -> HashMap<u64, Claim> {
    claims.iter().map(|claim| (claim.id, claim.clone())).collect()
}

/// Each section's title, its subsections, and the claims its questions rest on.
///
/// At most `MAX_SECTIONS`, one per title. When the outline cannot be read, each branch is a
/// section, titled by its first question. A question with claims that no section reports on adds
/// them to the section whose title is closest to it.
pub async fn outline(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    questions: &[Question],
) -> Result<Vec<(String, Vec<String>, BTreeSet<u64>)>, LlmError> {
    let held: Vec<&Question> = questions.iter().filter(|q| !q.claims.is_empty()).collect();
    let lines: Vec<String> = held
        .iter()
        .map(|q| {
            let name = if q.title.is_empty() { &q.text } else { &q.title };
            let status = status_name(q.status);
            let line =
                format!("[{}] {name}: {} ({status}, {} claims)", q.id, q.text, q.claims.len());
            if q.missing.is_empty() { line } else { format!("{line}; missing: {}", q.missing) }
        })
        .collect();
    let by_id: HashMap<u64, &Question> = held.iter().map(|q| (q.id, *q)).collect();
    let input = draft::Input { brief: brief.to_owned(), questions: lines.join("\n") };
    let drafted = match readable(tools.predict::<draft::Sig>(ctx, "outline", &input).await)? {
        Some(drafted) => drafted.sections,
        None => {
            tracing::warn!("could not read the outline");
            Vec::new()
        }
    };
    let usable: Vec<(String, Vec<String>, Vec<u64>)> = drafted
        .into_iter()
        .filter_map(|s| {
            let title = s.title.trim().to_owned();
            let asked: Vec<u64> = s
                .questions
                .iter()
                .filter_map(|&q| u64::try_from(q).ok())
                .filter(|q| by_id.contains_key(q))
                .collect();
            (!title.is_empty() && !asked.is_empty()).then_some((title, s.subsections, asked))
        })
        .collect();
    let drafted = if usable.is_empty() {
        branches(questions)
            .into_values()
            .map(|branch| {
                let first = branch[0];
                let title = if first.title.is_empty() { &first.text } else { &first.title };
                (title.clone(), Vec::new(), branch.iter().map(|q| q.id).collect())
            })
            .collect()
    } else {
        usable
    };
    // By title, lower-cased, so a repeated title adds to its first section.
    let mut found: IndexMap<String, (String, Vec<String>, BTreeSet<u64>)> = IndexMap::new();
    let mut unused: BTreeSet<u64> = by_id.keys().copied().collect();
    for (title, subsections, asked) in drafted {
        let asked: Vec<u64> = asked.into_iter().filter(|q| by_id.contains_key(q)).collect();
        let key = title.to_lowercase();
        if asked.is_empty() || (!found.contains_key(&key) && found.len() >= MAX_SECTIONS) {
            continue;
        }
        let (_, subs, ids) =
            found.entry(key).or_insert_with(|| (title, Vec::new(), BTreeSet::new()));
        for sub in subsections {
            let sub = sub.trim();
            if !sub.is_empty() && !subs.iter().any(|s| s == sub) {
                subs.push(sub.to_owned());
            }
        }
        for q in &asked {
            ids.extend(&by_id[q].claims);
            unused.remove(q);
        }
    }
    let mut sections: Vec<_> = found.into_values().collect();
    if !unused.is_empty() && !sections.is_empty() {
        let titles: Vec<String> = sections.iter().map(|(title, _, _)| title.clone()).collect();
        let vectors = tools.encoder.encode(&titles, false, ctx).await?;
        for left in unused {
            let question = by_id[&left];
            let closest = nearest(&tools.encoder, &question.text, &vectors, 1, ctx).await?[0];
            sections[closest].2.extend(&question.claims);
        }
    }
    Ok(sections)
}

/// The first questions the research did not settle, then the raised ones it answered in part or
/// found disputed. A raised question it found nothing for was a lead of its own, not the brief's.
pub fn open_questions(questions: &[Question]) -> String {
    let mut left: Vec<&Question> = questions
        .iter()
        .filter(|q| {
            unsettled(q.status).is_some()
                && (q.depth == 0
                    || matches!(q.status, QuestionStatus::Weak | QuestionStatus::Contradicted))
        })
        .collect();
    left.sort_by_key(|q| q.depth > 0);
    let lines: Vec<String> = left
        .iter()
        .take(OPEN_QUESTIONS)
        .map(|q| {
            let named = unsettled(q.status).filter(|_| q.attempts() > 0).unwrap_or("not searched");
            format!("- {} ({named})", q.text)
        })
        .collect();
    lines.join("\n")
}
