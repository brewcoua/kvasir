//! The question loop: search for one question at a time, judging each on what arrives.

use std::collections::{HashMap, HashSet};

use indexmap::IndexMap;
use kvasir_llm::LlmError;
use kvasir_research::claims::{Claim, closest, next_id, quoted_claims};
use kvasir_research::corpus::Corpus;
use kvasir_research::read::{read, recall};
use kvasir_research::search::{Ranking, search};
use kvasir_research::{ResearchError, Tools, readable};
use kvasir_run::RunCtx;
use kvasir_run::stages::RESEARCH;
use kvasir_sites::sources::DEADLINE;
use kvasir_types::results::{QuestionStatus, Row};
use kvasir_types::runs::Progress;

use crate::compare;
use crate::frontier::{BRANCH_STALL, Frontier, drop_claims, waiting};
use crate::questions::{MAX_QUESTIONS, Question, add_questions, as_results, status_name};
use crate::signatures::{Answer, NamedQuestion, check_answer, search_for};

/// The query shape for each attempt at a question, the last repeated: a question the plain words
/// do not answer gets the words its answer would contain, then a primary source, then the broader
/// topic. Whether `site:` and `filetype:` work depends on the engines SearXNG uses.
pub const QUERY_SHAPES: [&str; 4] = [
    "general: the plain words someone would search for",
    "specific: the exact terms, names, figures or year the answer would contain",
    "a primary source: add site: and the official domain, or filetype:pdf for a report or paper",
    "a step back: the broader topic the answer sits in",
];
/// Claims a question is judged on, the closest to it.
const CLAIMS_PER_QUESTION: usize = 8;
/// Searches in a row that add no claim before the research stops: one empty search is the cue to
/// try another shape, not to stop, so this waits for several.
pub const STALL_SEARCHES: usize = 4;
/// Searches in a row that fail before the run stops, rather than write from no evidence.
pub const FAILED_SEARCHES: usize = 3;
/// Follow-ups one answered question adds at most.
pub const FOLLOW_UPS: usize = 2;

/// What every pass of the research shares: the run's evidence, the pages it read, and how far it
/// searched.
pub struct Research<'a> {
    pub tools: &'a Tools,
    pub ctx: &'a RunCtx,
    pub today: String,
    pub brief: String,
    pub claims: Vec<Claim>,
    pub seen: HashSet<String>,
    pub corpus: Corpus,
    pub per_query: usize,
    pub max_attempts: usize,
    /// Searches made so far, by every pass.
    pub searched: usize,
    /// With a frontier, questions are taken in the order of their worth rather than breadth
    /// first, and a branch that stops adding claims or sources is closed.
    pub frontier: Option<Frontier>,
    /// The ranking or the table, whose cells lose the claims a purge removes.
    pub rows: Vec<Row>,
}

/// How a question is judged on the claims gathered so far, updating its status, claims and what
/// it misses.
pub enum Judge {
    /// By the `check` model, adding the follow-ups it asks for to the questions searched when
    /// `follow_ups` and the question is shallower than `max_depth`.
    Ledger { follow_ups: bool, max_depth: u64 },
    /// By the `check` model alone.
    Check,
    /// By assessing its cell again: the cell's place in `Research::rows`, by option and
    /// criterion.
    Cells(IndexMap<(String, String), (usize, usize)>),
}

/// Searches for `questions` (all of them, or `apart` instead when given), within `budget`
/// searches, then removes the claims of sites found `avoid` since they were read.
///
/// Events are published under `stage`, and attempts and recalls under the event `parent`. The
/// ledger is recorded on the run whenever a question in it changes; `apart` questions, as a
/// repair's are, are not part of it and are not recorded. With `from`, only the ledger's questions
/// from that index on are searched.
#[allow(clippy::too_many_arguments)]
pub async fn research(
    run: &mut Research<'_>,
    ledger: &mut Vec<Question>,
    apart: Option<&mut Vec<Question>>,
    from: usize,
    budget: usize,
    judge: &Judge,
    stage: &str,
    parent: Option<u64>,
) -> Result<(), ResearchError> {
    // After every pass, since a late answer may turn a site read earlier into an `avoid` one.
    match apart {
        Some(questions) => {
            run.searched +=
                search_questions(run, questions, 0, budget, judge, stage, parent, false).await?;
            purge(run, ledger.iter_mut().chain(questions.iter_mut())).await;
        }
        None => {
            run.searched +=
                search_questions(run, ledger, from, budget, judge, stage, parent, true).await?;
            purge(run, ledger.iter_mut()).await;
        }
    }
    Ok(())
}

/// Searches for `questions[from..]` one at a time, and returns how many searches that took.
///
/// Stops when `budget` searches are spent, no question is left to search for, or
/// `STALL_SEARCHES` searches in a row find nothing. A question is judged again before it is
/// searched for whenever claims arrived since its last judgement, since a search for one question
/// often answers another. Adds what it finds to the run's claims; `judge` may add to `questions`.
#[allow(clippy::too_many_arguments)]
pub async fn search_questions(
    run: &mut Research<'_>,
    questions: &mut Vec<Question>,
    from: usize,
    budget: usize,
    judge: &Judge,
    stage: &str,
    parent: Option<u64>,
    record: bool,
) -> Result<usize, ResearchError> {
    let (tools, ctx) = (run.tools, run.ctx);
    let record = |questions: &[Question]| {
        if record {
            ctx.run.record_questions(as_results(questions));
        }
    };
    let first_step = run.searched;
    let (mut searches, mut stalled, mut failed) = (0, 0, 0);
    // The pieces the corpus held when each question was last recalled for.
    let mut recalled: HashMap<u64, usize> = HashMap::new();
    let mut failures: Vec<String> = Vec::new();
    while searches < budget && stalled < STALL_SEARCHES {
        let picked = match run.frontier.as_mut() {
            Some(frontier) => frontier
                .pick(&tools.encoder, ctx, &questions[from..], &run.claims, run.max_attempts)
                .await?
                .map(|i| from + i),
            None => pick(&questions[from..], run.max_attempts).map(|i| from + i),
        };
        let Some(i) = picked else { break };
        // `judged_at` counts claims held, not ids: a purge leaves gaps in ids, not in the count.
        if questions[i].judged_at < run.claims.len() {
            judge_question(run, judge, questions, i).await?;
            record(questions);
            continue;
        }
        let (id, text) = (questions[i].id, questions[i].text.clone());
        // Pieces already downloaded, by this run or an earlier one, may answer it, and cost no
        // search; recall marks what it reads, so it is asked again only once the corpus has grown.
        if recalled.get(&id) != Some(&run.corpus.pieces.len()) {
            recalled.insert(id, run.corpus.pieces.len());
            let held = recall(
                tools,
                ctx,
                &mut run.corpus,
                &run.brief,
                &text,
                next_id(&run.claims),
                (first_step + searches) as u64,
                &format!("recall for {text}"),
                Some(id),
                parent,
                stage,
                Some(run.claims.len() as u64),
            )
            .await?;
            let found = held.len();
            run.claims.extend(held);
            if found > 0 {
                if let Some(frontier) = run.frontier.as_mut() {
                    frontier.merge(&tools.encoder, ctx, &mut run.claims, found).await?;
                }
                judge_question(run, judge, questions, i).await?;
                record(questions);
                if questions[i].status == QuestionStatus::Answered {
                    continue;
                }
            }
        }
        searches += 1;
        let attempt = ctx.publish(Progress {
            done: Some(searches as u64),
            total: Some(budget as u64),
            kind: Some("attempt".into()),
            parent,
            question: Some(id),
            ..Progress::new(stage, format!("search {searches} of at most {budget}, for: {text}"))
        });
        // A site that keeps giving confirmed claims is asked for this question, no plan needed.
        let mut query = None;
        if questions[i].attempts() > 0 {
            query = run.corpus.site_query(id, &text, &run.claims).await;
        }
        let query = match query {
            Some(query) => query,
            None => {
                let question = &questions[i];
                let input = search_for::Input {
                    today: run.today.clone(),
                    brief: run.brief.clone(),
                    question: text.clone(),
                    tried: if question.queries.is_empty() {
                        "none".into()
                    } else {
                        question.queries.join("\n")
                    },
                    missing: question.missing.clone(),
                    shape: QUERY_SHAPES[question.attempts().min(QUERY_SHAPES.len() - 1)].into(),
                };
                let planned = tools.predict::<search_for::Sig>(ctx, "plan", &input).await?;
                planned.query.trim().to_owned()
            }
        };
        let held = match run.frontier.as_mut() {
            Some(frontier) => Some(frontier.held(&tools.encoder, ctx, &run.claims).await?.to_vec()),
            None => None,
        };
        let ranking = Ranking {
            encoder: Some(&tools.encoder),
            question: &text,
            held: held.as_deref(),
            corpus: Some(&mut run.corpus),
            claims: &run.claims,
        };
        let queries = [query.clone()];
        let hits = match search(tools, ctx, &queries, run.per_query, &mut run.seen, ranking).await {
            Ok(hits) => hits,
            // The attempt is not spent, since the query was never answered; the budget is.
            Err(ResearchError::SearchUnavailable { failures: failed_now, .. }) => {
                failed += 1;
                failures.extend(failed_now);
                if failed == FAILED_SEARCHES {
                    return Err(ResearchError::unavailable(failed, failures));
                }
                continue;
            }
            Err(err) => return Err(err),
        };
        failed = 0;
        failures.clear();
        questions[i].queries.push(query);
        let new = read(
            tools,
            ctx,
            &mut run.corpus,
            &run.brief,
            &text,
            &hits,
            next_id(&run.claims),
            (first_step + searches) as u64,
            &format!("search {searches}"),
            Some(attempt),
            stage,
        )
        .await?;
        let found = new.len();
        run.claims.extend(new);
        if let Some(frontier) = run.frontier.as_mut()
            && frontier.searched(&tools.encoder, ctx, questions, i, &mut run.claims, found).await?
        {
            let detail = format!(
                "closing the branch of {text}: {BRANCH_STALL} searches in a row added nothing new"
            );
            ctx.publish(Progress::new(stage, detail));
        }
        stalled = if found > 0 { 0 } else { stalled + 1 };
        if found > 0 {
            judge_question(run, judge, questions, i).await?;
        }
        let question = &mut questions[i];
        if question.status == QuestionStatus::Open && question.attempts() >= run.max_attempts {
            question.status = QuestionStatus::Unresolved;
        }
        let status = status_name(question.status);
        // Before the event, so the status that follows it carries the questions as they now stand.
        record(questions);
        // done and total carry the claim counts here, not progress through the stage.
        let detail = format!(
            "search {searches}: {found} new claims, {} in all; {text}: {status}",
            run.claims.len()
        );
        ctx.publish(Progress {
            done: Some(found as u64),
            total: Some(run.claims.len() as u64),
            kind: Some("result".into()),
            parent: Some(attempt),
            ..Progress::new(stage, detail)
        });
    }
    Ok(searches)
}

/// Removes the claims of sites found `avoid` since they were read, waiting for Wikidata's late
/// answers. Their ids go from the cells of the run's rows too; a cell left with none is empty.
pub async fn purge<'a>(
    run: &mut Research<'_>,
    questions: impl IntoIterator<Item = &'a mut Question>,
) {
    let hosts = run.corpus.avoided(&run.claims, DEADLINE).await;
    if hosts.is_empty() {
        return;
    }
    let gone = match run.frontier.as_mut() {
        Some(frontier) => frontier.drop(&hosts, &mut run.claims, questions),
        None => drop_claims(&hosts, &mut run.claims, questions),
    };
    for cell in run.rows.iter_mut().flat_map(|row| row.cells.iter_mut()) {
        if cell.claims.iter().any(|id| gone.contains(id)) {
            cell.claims.retain(|id| !gone.contains(id));
            if cell.claims.is_empty() {
                cell.finding.clear();
            }
        }
    }
    tracing::warn!("dropped {} claims of sites found untrustworthy: {hosts:?}", gone.len());
    let detail = format!("dropped {} claims from sites found untrustworthy since", gone.len());
    run.ctx.publish(Progress::new(RESEARCH, detail));
}

/// The index of the question to search for next: the least tried of those not settled,
/// shallowest first.
///
/// Least tried first, so every question gets a plain search before any gets a narrower one.
pub fn pick(questions: &[Question], max_attempts: usize) -> Option<usize> {
    (0..questions.len())
        .filter(|&i| waiting(&questions[i], max_attempts))
        .min_by_key(|&i| (questions[i].attempts(), questions[i].depth, questions[i].id))
}

async fn judge_question(
    run: &mut Research<'_>,
    judge: &Judge,
    questions: &mut Vec<Question>,
    i: usize,
) -> Result<(), LlmError> {
    let (tools, ctx) = (run.tools, run.ctx);
    match judge {
        Judge::Ledger { follow_ups, max_depth } => {
            let asked = check(tools, ctx, &run.brief, &run.claims, &mut questions[i]).await?;
            let question = &questions[i];
            if *follow_ups
                && question.status == QuestionStatus::Answered
                && question.depth < *max_depth
            {
                let untitled: Vec<NamedQuestion> = asked
                    .into_iter()
                    .take(FOLLOW_UPS)
                    .map(|question| NamedQuestion { question, ..NamedQuestion::default() })
                    .collect();
                add_questions(&tools.encoder, ctx, questions, &untitled, Some(i), MAX_QUESTIONS)
                    .await?;
            }
        }
        Judge::Check => {
            check(tools, ctx, &run.brief, &run.claims, &mut questions[i]).await?;
        }
        Judge::Cells(cells) => {
            let question = &mut questions[i];
            let key = question.cell.as_ref().expect("a cell's question");
            let (row, at) = cells[key];
            let texts: Vec<String> = run.claims.iter().map(|c| c.text.clone()).collect();
            let vectors = tools.encoder.encode(&texts, false, ctx).await?;
            let (entity, criterion) = key;
            let cell =
                compare::cell(tools, ctx, &vectors, &run.brief, &run.claims, entity, criterion)
                    .await?;
            question.judged_at = run.claims.len();
            question.claims.clone_from(&cell.claims);
            question.status = if question.claims.is_empty() {
                QuestionStatus::Open
            } else {
                QuestionStatus::Answered
            };
            run.rows[row].cells[at] = cell;
        }
    }
    Ok(())
}

/// Judges `question` on its closest claims, and returns the harder questions the judge asks.
///
/// A claim whose quote does not say what it claims is not counted, so a misquoted answer leaves
/// the question open and the next search looks for it again.
pub async fn check(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    claims: &[Claim],
    question: &mut Question,
) -> Result<Vec<String>, LlmError> {
    let previous = question.judged_at;
    question.judged_at = claims.len();
    let texts: Vec<String> = claims.iter().map(|c| c.text.clone()).collect();
    let shown: Vec<&Claim> =
        closest(&tools.encoder, &question.text, &texts, CLAIMS_PER_QUESTION, ctx)
            .await?
            .into_iter()
            .map(|i| &claims[i])
            .collect();
    // Ids count up from 1 as claims arrive, so none above `previous` means the closest claims are
    // the ones it was last judged on, and the verdict would not change.
    if shown.iter().all(|c| c.id <= previous as u64) {
        return Ok(Vec::new());
    }
    let input = check_answer::Input {
        brief: brief.to_owned(),
        question: question.text.clone(),
        evidence: quoted_claims(shown.iter().copied()),
    };
    let Some(judged) = readable(tools.predict::<check_answer::Sig>(ctx, "check", &input).await)?
    else {
        return Ok(Vec::new());
    };
    // Only ids it was shown: a small model otherwise cites claims from its own numbering.
    let ids: Vec<u64> = shown
        .iter()
        .map(|c| c.id)
        .filter(|&id| judged.claims.iter().any(|&j| u64::try_from(j) == Ok(id)))
        .collect();
    if judged.status == Answer::Open || ids.is_empty() {
        question.status = QuestionStatus::Open;
        question.claims.clear();
    } else {
        question.status = match judged.status {
            Answer::Answered => QuestionStatus::Answered,
            Answer::Weak => QuestionStatus::Weak,
            Answer::Contradicted => QuestionStatus::Contradicted,
            Answer::Open => QuestionStatus::Open,
        };
        question.claims = ids;
    }
    let answered = question.status == QuestionStatus::Answered;
    question.missing = if answered { String::new() } else { judged.missing.trim().to_owned() };
    Ok(if answered { judged.follow_ups } else { Vec::new() })
}
