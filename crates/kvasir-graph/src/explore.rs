//! Exploring the graph: search for the question proof numbers pick, take in its claims, ask what
//! they raise.
//!
//! Each step reads only the question's path from its first question and the evidence found for it,
//! never the whole graph, so its input stays small however large the graph grows. A question is not
//! judged again on what other branches find: that would read the whole graph. It is judged again on
//! what its own children find, once they are all answered.

use std::collections::{BTreeSet, HashMap};

use kvasir_ledger::frontier::{BRANCH_STALL, Frontier};
use kvasir_ledger::questions::{Question, add_questions, as_results, status_name};
use kvasir_ledger::search_loop::{FAILED_SEARCHES, QUERY_SHAPES, Research};
use kvasir_ledger::signatures::{Answer, NamedQuestion, search_for};
use kvasir_llm::LlmError;
use kvasir_research::claims::{Claim, next_id, quoted_claims};
use kvasir_research::read::{read, recall};
use kvasir_research::search::{Ranking, search};
use kvasir_research::{ResearchError, Tools, readable};
use kvasir_run::RunCtx;
use kvasir_run::stages::RESEARCH;
use kvasir_types::results::QuestionStatus;
use kvasir_types::runs::Progress;
use tokio::time::Instant;

use crate::graph::path;
use crate::proof::{children, pick};
use crate::signatures::{expand, gaps};

/// Progressive widening (Couëtoux et al. 2011): a question not yet answered, searched n times, may
/// hold at most ceil(WIDEN * n ** ALPHA) children, and never more than MAX_CHILDREN; one that keeps
/// coming back weak earns more. An answered question holds at most FOLLOW_UPS, a harder question
/// going deeper, so the research does not end once the first questions are answered. Untuned first
/// guesses.
const WIDEN: f64 = 2.0;
const ALPHA: f64 = 0.5;
const MAX_CHILDREN: usize = 4;
const FOLLOW_UPS: usize = 1;
/// New first questions one gap call adds at most, and dry gap calls in a row before the research
/// ends. A gap call is made once no question can still be settled and time is left. It is dry when
/// it adds no question, or the questions it added found fewer than `GAP_YIELD` claims a search: the
/// research has drifted from the brief to questions the web says little about.
const GAP_QUESTIONS: usize = 3;
pub const GAP_STALL: usize = 2;
const GAP_YIELD: usize = 2;
/// Questions in the graph at most. The time budget bounds what is searched; this bounds the input
/// of the writing and the size of the graph shown.
pub const MAX_QUESTIONS: usize = 80;
/// The share of the research time, at its end, in which no question raises others and no gap call
/// is made: a question asked then would go unsearched. See `proof` for what is searched instead.
pub const LATE_SHARE: f64 = 0.25;

/// Explores from `questions`, and returns how many searches that took.
///
/// Stops at `deadline`, once `budget` searches are spent when there is one, or once `GAP_STALL`
/// gap calls in a row are dry. A question raises others only above `max_depth`, and only before
/// `late_at`, after which the research stops once no question can still be settled.
#[allow(clippy::too_many_arguments)]
pub async fn explore(
    run: &mut Research<'_>,
    frontier: &mut Frontier,
    questions: &mut Vec<Question>,
    deadline: Instant,
    late_at: Instant,
    budget: Option<usize>,
    max_depth: u64,
) -> Result<usize, ResearchError> {
    let (tools, ctx) = (run.tools, run.ctx);
    let record = |questions: &[Question]| ctx.run.record_questions(as_results(questions));
    // The pieces the corpus held when each question was last recalled for.
    let mut recalled: HashMap<u64, usize> = HashMap::new();
    let (mut searches, mut failed, mut stalled) = (0, 0, 0);
    let mut failures: Vec<String> = Vec::new();
    // Searches and claims when the last gap call was made.
    let mut gapped: Option<(usize, usize)> = None;
    loop {
        let now = Instant::now();
        if now >= deadline || budget.is_some_and(|budget| searches >= budget) {
            break;
        }
        let late = now >= late_at;
        let picked =
            pick(frontier, &tools.encoder, ctx, questions, &run.claims, run.max_attempts, late)
                .await?;
        let Some(i) = picked else {
            if late {
                let detail = "stopping: no question left to settle, and too little time";
                ctx.publish(Progress::new(RESEARCH, detail));
                break;
            }
            if let Some((at, held)) = gapped {
                let spent = searches - at;
                if spent > 0 {
                    let found = run.claims.len() - held;
                    stalled = if found < GAP_YIELD * spent { stalled + 1 } else { 0 };
                }
            }
            if stalled >= GAP_STALL {
                let detail =
                    format!("stopping: the last {GAP_STALL} gaps found little or nothing new");
                ctx.publish(Progress::new(RESEARCH, detail));
                break;
            }
            gapped = Some((searches, run.claims.len()));
            let asked = questions.len();
            let raised = lacking(tools, ctx, &run.brief, questions).await?;
            add_questions(&tools.encoder, ctx, questions, &raised, None, MAX_QUESTIONS).await?;
            if questions.len() == asked {
                stalled += 1;
            }
            record(questions);
            let detail = format!(
                "no question left to settle; asked what the report still lacks: {} new questions",
                questions.len() - asked
            );
            ctx.publish(Progress::new(RESEARCH, detail));
            continue;
        };
        let (id, text) = (questions[i].id, questions[i].text.clone());
        // As in the ledger loop: pieces already downloaded may answer it, and cost no search.
        if recalled.get(&id) != Some(&run.corpus.pieces.len()) {
            recalled.insert(id, run.corpus.pieces.len());
            let held = recall(
                tools,
                ctx,
                &mut run.corpus,
                &run.brief,
                &text,
                next_id(&run.claims),
                searches as u64,
                &format!("recall for {text}"),
                Some(id),
                None,
                RESEARCH,
                Some(run.claims.len() as u64),
            )
            .await?;
            let found = held.len();
            run.claims.extend(held);
            if found > 0 {
                frontier.merge(&tools.encoder, ctx, &mut run.claims, found).await?;
                take_in(run, questions, i, found, late, max_depth).await?;
                record(questions);
                if questions[i].status == QuestionStatus::Answered {
                    continue;
                }
            }
        }
        searches += 1;
        let left = (deadline.saturating_duration_since(Instant::now()).as_secs_f64() / 60.0).ceil();
        let attempt = ctx.publish(Progress {
            done: Some(searches as u64),
            total: budget.map(|budget| budget as u64),
            kind: Some("attempt".into()),
            question: Some(id),
            ..Progress::new(RESEARCH, format!("search {searches}, {left} min left, for: {text}"))
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
        let held = frontier.held(&tools.encoder, ctx, &run.claims).await?.to_vec();
        let ranking = Ranking {
            encoder: Some(&tools.encoder),
            question: &text,
            held: Some(&held),
            corpus: Some(&mut run.corpus),
            claims: &run.claims,
        };
        let queries = [query.clone()];
        let hits = match search(tools, ctx, &queries, run.per_query, &mut run.seen, ranking).await {
            Ok(hits) => hits,
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
            searches as u64,
            &format!("search {searches}"),
            Some(attempt),
            RESEARCH,
        )
        .await?;
        let found = new.len();
        run.claims.extend(new);
        if frontier.searched(&tools.encoder, ctx, questions, i, &mut run.claims, found).await? {
            let detail = format!(
                "closing the branch of {text}: {BRANCH_STALL} searches in a row added nothing new"
            );
            ctx.publish(Progress::new(RESEARCH, detail));
        }
        if found > 0 {
            take_in(run, questions, i, found, late, max_depth).await?;
        }
        let question = &mut questions[i];
        if question.status == QuestionStatus::Open && question.attempts() >= run.max_attempts {
            question.status = QuestionStatus::Unresolved;
        }
        let status = status_name(question.status);
        // Before the event, so the status that follows it carries the graph as it now stands.
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
            ..Progress::new(RESEARCH, detail)
        });
    }
    Ok(searches)
}

/// Judges the question at `i` on the last `new` claims of the run, adds the questions that raises,
/// and settles what it answers.
async fn take_in(
    run: &Research<'_>,
    questions: &mut Vec<Question>,
    i: usize,
    new: usize,
    late: bool,
    max_depth: u64,
) -> Result<(), LlmError> {
    let (tools, ctx) = (run.tools, run.ctx);
    let found = &run.claims[run.claims.len() - new..];
    let raised = judge(tools, ctx, &run.brief, questions, &run.claims, i, found).await?;
    let question = &questions[i];
    let room = if question.status == QuestionStatus::Answered {
        FOLLOW_UPS
    } else {
        MAX_CHILDREN.min((WIDEN * (question.attempts() as f64).powf(ALPHA)).ceil() as usize)
    };
    let held = children(questions)[&question.id].len();
    if question.depth < max_depth && room > held && !late {
        let raised = &raised[..raised.len().min(room - held)];
        add_questions(&tools.encoder, ctx, questions, raised, Some(i), MAX_QUESTIONS).await?;
    }
    settle(tools, ctx, &run.brief, questions, &run.claims, i).await
}

/// New first questions on what the questions asked so far leave out of the brief.
async fn lacking(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    questions: &[Question],
) -> Result<Vec<NamedQuestion>, LlmError> {
    let listed: Vec<String> = questions
        .iter()
        .map(|q| {
            let name = if q.title.is_empty() { &q.text } else { &q.title };
            format!("{name}: {} ({})", q.text, status_name(q.status))
        })
        .collect();
    let input = gaps::Input { brief: brief.to_owned(), questions: listed.join("\n") };
    let Some(found) = readable(tools.predict::<gaps::Sig>(ctx, "decompose", &input).await)? else {
        tracing::warn!("could not read what the report still lacks");
        return Ok(Vec::new());
    };
    let mut raised = found.raised;
    raised.truncate(GAP_QUESTIONS);
    Ok(raised)
}

/// Once the question at `i` is answered and so is every question beside it, judges their parent
/// again on what they rest on, and so on up while each parent is answered in turn.
async fn settle(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    questions: &mut [Question],
    claims: &[Claim],
    mut i: usize,
) -> Result<(), LlmError> {
    while questions[i].status == QuestionStatus::Answered {
        let Some(parent) = questions[i].parent else { return Ok(()) };
        let p = questions.iter().position(|q| q.id == parent).expect("a parent in the graph");
        let below = &children(questions)[&parent];
        if questions[p].status == QuestionStatus::Answered
            || below.iter().any(|&c| questions[c].status != QuestionStatus::Answered)
        {
            return Ok(());
        }
        let held: BTreeSet<u64> = below.iter().flat_map(|&c| questions[c].claims.clone()).collect();
        let rest: Vec<Claim> = claims
            .iter()
            .filter(|c| held.contains(&c.id) && !questions[p].claims.contains(&c.id))
            .cloned()
            .collect();
        // Raises nothing: a parent its children answered has nothing left to raise.
        judge(tools, ctx, brief, questions, claims, p, &rest).await?;
        i = p;
    }
    Ok(())
}

/// Judges the question at `i` on the claims it already rests on and the `new` ones its search
/// just found, and returns the questions they raise.
async fn judge(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    questions: &mut [Question],
    claims: &[Claim],
    i: usize,
    new: &[Claim],
) -> Result<Vec<NamedQuestion>, LlmError> {
    let question = &questions[i];
    let mut shown: Vec<&Claim> =
        claims.iter().filter(|c| question.claims.contains(&c.id)).collect();
    shown.extend(new);
    let above: Vec<&str> = path(questions, question).iter().map(|q| q.text.as_str()).collect();
    let input = expand::Input {
        brief: brief.to_owned(),
        path: if above.is_empty() { "none".into() } else { above.join("\n") },
        question: question.text.clone(),
        evidence: quoted_claims(shown.iter().copied()),
    };
    let Some(judged) = readable(tools.predict::<expand::Sig>(ctx, "check", &input).await)? else {
        tracing::warn!("could not read the judgement on {:?}", question.text);
        return Ok(Vec::new());
    };
    // Only ids it was shown: a small model otherwise cites claims from its own numbering.
    let ids: Vec<u64> = shown
        .iter()
        .map(|c| c.id)
        .filter(|&id| judged.claims.iter().any(|&j| u64::try_from(j) == Ok(id)))
        .collect();
    let question = &mut questions[i];
    question.judged_at = claims.len();
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
    Ok(judged.raised)
}
