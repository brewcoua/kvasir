//! Reading the pages a search found into claims, each with a quote that is on its page.
//!
//! With a grounding checker, each claim must also be supported by the piece of the page it was read
//! from: tier 2 of verification, after the quote check that is tier 1.

use std::sync::Arc;

use futures::{StreamExt, stream};
use indexmap::IndexMap;
use kvasir_extract::{PIECE_CHARACTERS, split};
use kvasir_fetch::Page;
use kvasir_llm::{LlmError, Target};
use kvasir_run::RunCtx;
use kvasir_types::runs::Progress;

use crate::claims::Claim;
use crate::corpus::Corpus;
use crate::encoder::cosine;
use crate::ground::grounded;
use crate::search::{Hit, descending};
use crate::signatures::intake as sig;
use crate::text::{misleading, quoted, with_table_header};
use crate::{Tools, readable};

/// How much of one page the embedding filter keeps, the only part a model reads: the 3 pieces of
/// 6,000 characters it kept before pages were split at their headings, whose pieces are smaller.
pub const PAGE_CHARACTERS: usize = 3 * PIECE_CHARACTERS;
// What an intake call holds besides the page, and how many characters a token is, from the 47
// intake calls of one run on gpt-oss: prompt tokens came to 34 + characters / 4.07, with brief,
// instructions and title about 600. Both are rounded towards a smaller page, as is the margin: a
// token holds fewer characters in figures, PDFs and languages other than English.
const PROMPT_TOKENS: usize = 1000;
const CHARACTERS_PER_TOKEN: f64 = 3.5;
const MARGIN: f64 = 0.9;
/// Joins the pieces of a page read in one call. They need not be adjacent on the page, so they are
/// marked apart, and a claim is grounded on the one piece its quote is in.
pub const GAP: &str = "\n\n[...]\n\n";

/// The room an intake call's answer gets: a quarter of the context. One run's answers peaked at
/// about 500 tokens for one piece, and one call now reads up to five.
pub fn answer_tokens(context_tokens: usize) -> usize {
    context_tokens / 4
}

/// How much of a page one intake call reads: what the context holds once the prompt and the
/// answer are taken out, less the margin.
pub fn page_characters(context_tokens: usize) -> usize {
    let tokens =
        context_tokens as f64 - PROMPT_TOKENS as f64 - answer_tokens(context_tokens) as f64;
    (tokens * MARGIN * CHARACTERS_PER_TOKEN).max(0.0) as usize
}

/// `pieces` joined into as few texts of at most `size` characters as their order allows. A piece
/// longer than `size` is a text of its own.
pub fn pack(pieces: &[String], size: usize) -> Vec<String> {
    let gap = GAP.chars().count();
    let mut texts: Vec<(Vec<&str>, usize)> = Vec::new();
    for piece in pieces {
        let length = piece.chars().count();
        match texts.last_mut() {
            Some((text, held)) if *held + gap + length <= size => {
                text.push(piece);
                *held += gap + length;
            }
            _ => texts.push((vec![piece], length)),
        }
    }
    texts.into_iter().map(|(text, _)| text.join(GAP)).collect()
}

/// A text of a page for one intake call: where it was read, and whether it is a search snippet.
struct Reading {
    url: String,
    title: String,
    text: String,
    from_snippet: bool,
}

impl Reading {
    fn name(&self) -> &str {
        if self.title.is_empty() { &self.url } else { &self.title }
    }
}

/// The claims in the pieces of each page closest to `question`, the one it was found for.
///
/// Kept by the question rather than the brief: a page found for one narrow fact holds it in one
/// piece, which the brief would not rank first. Each read is published under the event `attempt`,
/// the one that began the search. Every piece of a page read goes into `corpus`, and those read
/// are marked.
#[allow(clippy::too_many_arguments)]
pub async fn read(
    tools: &Tools,
    ctx: &RunCtx,
    corpus: &mut Corpus,
    brief: &str,
    question: &str,
    hits: &[Hit],
    first_id: u64,
    step: u64,
    label: &str,
    attempt: Option<u64>,
    stage: &str,
) -> Result<Vec<Claim>, LlmError> {
    let mut pages = Vec::new();
    for (hit, fetched) in hits.iter().zip(fetch_all(tools, hits).await) {
        let mut title = hit.title.clone();
        let (parts, from_snippet) = match fetched {
            Err(snippet) => (split(&snippet, PIECE_CHARACTERS), true),
            Ok(page) => {
                // A lead's title is the anchor that linked it, which the page's own replaces.
                if hit.depth.is_some() && !page.title.is_empty() {
                    title.clone_from(&page.title);
                }
                let depth = hit.depth.unwrap_or(0) + 1;
                (relevant_pieces(tools, ctx, corpus, question, &title, page, depth).await?, false)
            }
        };
        pages.push((hit.url.clone(), title, parts, from_snippet));
    }
    let size = page_characters(tools.context_tokens);
    let mut numbers = Vec::new();
    let mut texts = Vec::new();
    for (page, (url, title, parts, from_snippet)) in pages.iter().enumerate() {
        for text in pack(parts, size) {
            numbers.push(page + 1);
            let (url, title, from_snippet) = (url.clone(), title.clone(), *from_snippet);
            texts.push(Reading { url, title, text, from_snippet });
        }
    }
    let publish = |i: usize, done: usize| {
        let page = numbers[i];
        let parts = numbers.iter().filter(|&&p| p == page).count();
        let detail = format!("{label}: read page {page}/{}: {}", pages.len(), texts[i].name());
        ctx.publish(Progress {
            done: Some(done as u64),
            total: Some(texts.len() as u64),
            kind: Some(if parts > 1 { "part" } else { "page" }.into()),
            parent: attempt,
            page: Some(page as u64),
            ..Progress::new(stage, detail)
        });
    };
    take_all(tools, ctx, brief, question, &texts, first_id, step, publish).await
}

/// The claims in the unread pieces of `corpus` closest to `question`, read with no search.
///
/// The pieces of a page are read together, as [`read`] reads them, and are marked read by the
/// recall that returns them, so a question never reads the same piece twice. When it finds pieces
/// it publishes a `recall` event under `parent`, a `part` event under it for each text read, and a
/// `result` event under it with the claims added and the total, counted from `claims_held` (by
/// default `first_id - 1`); when it finds none, nothing.
#[allow(clippy::too_many_arguments)]
pub async fn recall(
    tools: &Tools,
    ctx: &RunCtx,
    corpus: &mut Corpus,
    brief: &str,
    question: &str,
    first_id: u64,
    step: u64,
    label: &str,
    question_id: Option<u64>,
    parent: Option<u64>,
    stage: &str,
    claims_held: Option<u64>,
) -> Result<Vec<Claim>, LlmError> {
    let mut pages: IndexMap<String, (Arc<Page>, Vec<String>)> = IndexMap::new();
    for (found, text) in corpus.recall(&tools.encoder, question, ctx).await? {
        pages.entry(found.url.clone()).or_insert_with(|| (found, Vec::new())).1.push(text);
    }
    if pages.is_empty() {
        return Ok(Vec::new());
    }
    let size = page_characters(tools.context_tokens);
    let mut numbers = Vec::new();
    let mut texts = Vec::new();
    for (number, (found, parts)) in pages.values().enumerate() {
        for text in pack(parts, size) {
            numbers.push(number + 1);
            // The URL the page answered at: a page from the cache has its normalized one as `url`.
            let (url, title) = (found.final_url.clone(), found.title.clone());
            texts.push(Reading { url, title, text, from_snippet: false });
        }
    }
    let held: usize = pages.values().map(|(_, parts)| parts.len()).sum();
    let recalled = ctx.publish(Progress {
        done: Some(held as u64),
        total: Some(pages.len() as u64),
        kind: Some("recall".into()),
        parent,
        question: question_id,
        ..Progress::new(stage, format!("{label}: {held} pieces from {} pages", pages.len()))
    });
    let publish = |i: usize, done: usize| {
        let detail =
            format!("{label}: read page {}/{}: {}", numbers[i], pages.len(), texts[i].name());
        ctx.publish(Progress {
            done: Some(done as u64),
            total: Some(texts.len() as u64),
            kind: Some("part".into()),
            parent: Some(recalled),
            page: Some(numbers[i] as u64),
            ..Progress::new(stage, detail)
        });
    };
    let new = take_all(tools, ctx, brief, question, &texts, first_id, step, publish).await?;
    corpus.record(&new);
    let total = claims_held.unwrap_or(first_id - 1) + new.len() as u64;
    ctx.publish(Progress {
        done: Some(new.len() as u64),
        total: Some(total),
        kind: Some("result".into()),
        parent: Some(recalled),
        ..Progress::new(stage, format!("{label}: {} new claims, {total} in all", new.len()))
    });
    Ok(new)
}

/// The claims in each of `texts`, numbered from `first_id` in order. `publish` is told each read
/// as it finishes.
#[allow(clippy::too_many_arguments)]
async fn take_all(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    question: &str,
    texts: &[Reading],
    first_id: u64,
    step: u64,
    publish: impl Fn(usize, usize),
) -> Result<Vec<Claim>, LlmError> {
    // Intake is most of a run's time, so its calls run side by side, as wide as the pools, and each
    // is its own event: the page read is what a follower wants to see. Made before any runs, so
    // each call names the event current now, as a pool task would.
    let calls: Vec<_> = texts
        .iter()
        .map(|reading| {
            let ctx = ctx.clone();
            async move { intake(tools, &ctx, brief, question, reading, step).await }
        })
        .collect();
    let mut answers = stream::iter(calls).buffered(tools.max_threads.max(1));
    let mut taken = Vec::new();
    let mut i = 0;
    while let Some(found) = answers.next().await {
        let found = found?;
        publish(i, i + 1);
        taken.push((&texts[i].text, found));
        i += 1;
    }
    let checker = tools.targets.get("ground_intake");
    let mut new: Vec<Claim> = Vec::new();
    // Grounded once every page is read, not after each: the checker and the intake model share one
    // local server, and alternating between them would load a model for each page.
    for (text, found) in taken {
        let found = match checker {
            Some(checker) => supported(tools, ctx, checker, text, found).await?,
            None => found,
        };
        // Numbered in page order once read, however the calls finished.
        for mut claim in found {
            claim.id = first_id + new.len() as u64;
            new.push(claim);
        }
    }
    Ok(new)
}

/// Each page, or as `Err` its search snippet when the page cannot be read. A lead has no snippet:
/// its description is what another page says of it, so one that cannot be read gives no text.
async fn fetch_all(tools: &Tools, hits: &[Hit]) -> Vec<Result<Page, String>> {
    let fetches = hits.iter().map(|hit| async move {
        tools.pages.fetch(&hit.url, tools.max_age).await.map_err(|err| {
            tracing::info!("falling back to the snippet: {err}");
            if hit.depth.is_some() { String::new() } else { hit.description.clone() }
        })
    });
    stream::iter(fetches).buffered(tools.max_threads.max(1)).collect().await
}

/// The pieces of `page` closest to `query`, in page order, as many as `PAGE_CHARACTERS` holds.
///
/// All of them go into `corpus`, encoded once, each with the page's title, which the piece itself
/// may not name; a model reads the piece alone. The ones returned are marked read. The links of the
/// page become leads of depth `lead_depth`.
pub async fn relevant_pieces(
    tools: &Tools,
    ctx: &RunCtx,
    corpus: &mut Corpus,
    query: &str,
    title: &str,
    page: Page,
    lead_depth: u32,
) -> Result<Vec<String>, LlmError> {
    let pieces = split(&page.text, PIECE_CHARACTERS);
    let titled: Vec<String> = pieces.iter().map(|piece| format!("{title}\n{piece}")).collect();
    let vectors = tools.encoder.encode(&titled, false, ctx).await?;
    let page = Arc::new(page);
    let added = corpus.add(Arc::clone(&page), pieces.clone(), vectors).await;
    corpus.add_leads(&tools.encoder, &page, lead_depth, ctx).await?;
    let mut kept: Vec<usize> = (0..pieces.len()).collect();
    if pieces.iter().map(|piece| piece.chars().count()).sum::<usize>() > PAGE_CHARACTERS {
        kept.clear();
        let mut length = 0;
        let wanted = tools.encoder.one(query, true, ctx).await?;
        let scores: Vec<f64> =
            added.clone().map(|n| cosine(&wanted, &corpus.pieces[n].vector)).collect();
        for i in descending(&scores) {
            let size = pieces[i].chars().count();
            if length + size > PAGE_CHARACTERS {
                continue;
            }
            kept.push(i);
            length += size;
        }
    }
    for &i in &kept {
        corpus.pieces[added.start + i].read = true;
    }
    kept.sort_unstable();
    Ok(kept.into_iter().map(|i| pieces[i].clone()).collect())
}

/// Claims whose quote really is in the text. A paraphrased quote is not evidence, nor is one that a
/// negator or qualifier just before it, or inside its ellipsis, turns around.
///
/// Checked against the text rather than the quote alone: a claim names what its quote leaves to
/// the surrounding text, such as the subject of "it was found in 1904". Ids are left at 0.
async fn intake(
    tools: &Tools,
    ctx: &RunCtx,
    brief: &str,
    question: &str,
    reading: &Reading,
    step: u64,
) -> Result<Vec<Claim>, LlmError> {
    let input = sig::Input {
        brief: brief.to_owned(),
        question: question.to_owned(),
        title: reading.title.clone(),
        text: reading.text.clone(),
    };
    let Some(answer) = readable(tools.predict::<sig::Sig>(ctx, "intake", &input).await)? else {
        return Ok(Vec::new());
    };
    let piece = &reading.text;
    let mut claims = Vec::new();
    for finding in answer.findings {
        if !quoted(&finding.quote, piece) {
            tracing::info!(
                "dropped a claim whose quote is not on {}: {:?}",
                reading.url,
                finding.quote
            );
            continue;
        }
        if misleading(&finding.quote, source(&finding.quote, piece)) {
            tracing::info!(
                "dropped a claim whose quote leaves out a negation: {:?}",
                finding.quote
            );
            continue;
        }
        claims.push(Claim {
            id: 0,
            text: finding.claim,
            // After the checks: the header's ellipsis would make `misleading` read the rows between.
            quote: with_table_header(&finding.quote, piece),
            url: reading.url.clone(),
            title: reading.title.clone(),
            confidence: finding.confidence,
            step,
            from_snippet: reading.from_snippet,
            corroborated_by: Vec::new(),
            contradicted_by: Vec::new(),
        });
    }
    Ok(claims)
}

/// Those of `claims`, read from `piece`, that the part of the piece their quote is in supports.
async fn supported(
    tools: &Tools,
    ctx: &RunCtx,
    checker: &Target,
    piece: &str,
    claims: Vec<Claim>,
) -> Result<Vec<Claim>, LlmError> {
    let mut kept = Vec::new();
    for claim in claims {
        if grounded(tools, ctx, checker, source(&claim.quote, piece), &claim.text).await? {
            kept.push(claim);
        } else {
            tracing::info!("dropped a claim {} does not support: {:?}", claim.url, claim.text);
        }
    }
    Ok(kept)
}

/// The part of `piece` that `quote` is in, which is all a small checker has room for.
pub fn source<'a>(quote: &str, piece: &'a str) -> &'a str {
    piece.split(GAP).find(|part| quoted(quote, part)).unwrap_or(piece)
}
