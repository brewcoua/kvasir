//! STORM: an article written from simulated conversations between editors and an expert who
//! searches to answer them. Stanford OVAL's algorithm and prompts, MIT licensed; see `NOTICE`.
//!
//! The stages are `research`, `outline`, `article` and, unless turned off, `polish`. Each editor
//! holds a conversation side by side with the others; the sections are written side by side too.
//! STORM reads search snippets only, not pages.

pub mod article;
pub mod curation;
pub mod signatures;
pub mod text;
pub mod write;

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use futures::StreamExt;
use kvasir_config::{ConfigError, Settings, StormSettings};
use kvasir_fetch::Web;
use kvasir_llm::{LlmError, Target, roles};
use kvasir_research::Tools;
use kvasir_run::RunCtx;
use kvasir_run::stages::{ARTICLE, OUTLINE, POLISH, RESEARCH};
use kvasir_types::requests::{Input, StormOptions};
use kvasir_types::results::{ArticleResult, Citation};
use kvasir_types::runs::Progress;

use crate::article::Article;
use crate::curation::{converse, editors, sources};
use crate::text::parse_article;
use crate::write::{
    Snippets, conversation, outlines, write_lead, write_section, written_by_polishing,
};

#[derive(Debug, thiserror::Error)]
pub enum StormError {
    #[error(transparent)]
    Llm(#[from] LlmError),
    #[error("no article was written")]
    NoArticle,
}

/// One target per STORM role, with `fast` and `strong` in place of the configured models where
/// given.
pub fn targets(
    settings: &Settings,
    fast: Option<&str>,
    strong: Option<&str>,
) -> Result<HashMap<&'static str, Target>, ConfigError> {
    let mut settings = settings.clone();
    if let Some(fast) = fast.filter(|name| !name.is_empty()) {
        settings.model.fast = fast.to_owned();
    }
    if let Some(strong) = strong.filter(|name| !name.is_empty()) {
        settings.model.strong = strong.to_owned();
    }
    roles::STORM
        .iter()
        .map(|&(role, tier, max_tokens)| {
            Ok((role, roles::target(&settings, role, tier, max_tokens)?))
        })
        .collect()
}

fn counted(stage: &str, detail: String, done: u64, total: u64, series: Option<&str>) -> Progress {
    Progress {
        done: Some(done),
        total: Some(total),
        series: series.map(str::to_owned),
        ..Progress::new(stage, detail)
    }
}

/// Writes an article on the question of `input`, `defaults` holding the budgets `options` leave
/// out. The pages a model names for its editors are read through `web`.
pub async fn run(
    tools: &Tools,
    web: &Web,
    defaults: &StormSettings,
    ctx: &RunCtx,
    input: &Input,
    options: &StormOptions,
) -> Result<ArticleResult, StormError> {
    let started = Instant::now();
    let topic = input.question.as_str();
    let max_turns = options.max_conv_turn.map_or(defaults.max_conv_turn, |n| n as usize);
    let max_editors = options.max_perspective.map_or(defaults.max_perspective, |n| n as usize);

    ctx.publish(Progress::new(RESEARCH, "identifying perspectives"));
    let editors = editors(tools, web, ctx, topic, max_editors).await?;
    let most = (editors.len() * max_turns) as u64;
    ctx.publish(Progress::new(RESEARCH, format!("identified {} perspectives", editors.len())));
    ctx.publish(Progress::new(RESEARCH, "searching and asking questions"));
    let turns = AtomicU64::new(0);
    let turned = |ctx: &RunCtx| {
        let done = turns.fetch_add(1, Ordering::Relaxed) + 1;
        let detail = format!("completed conversation turn {done}");
        ctx.publish(counted(RESEARCH, detail, done, most, Some("turns")));
    };
    // Collected first: a lazy map left in the stream keeps the future from being Send.
    let held: Vec<_> = editors
        .iter()
        .map(|editor| {
            let (ctx, turned) = (ctx.clone(), &turned);
            async move { converse(tools, &ctx, topic, editor, max_turns, turned).await }
        })
        .collect();
    let width = tools.max_threads.clamp(1, editors.len().max(1));
    let held: Vec<_> = futures::stream::iter(held).buffered(width).collect().await;
    let mut conversations = Vec::new();
    for (editor, turns) in editors.into_iter().zip(held) {
        conversations.push((editor, turns?));
    }
    let total = turns.load(Ordering::Relaxed);
    let detail = format!("gathered information over {total} turns");
    ctx.publish(counted(RESEARCH, detail, total, total, None));

    ctx.publish(Progress::new(OUTLINE, "organising what was found"));
    let all: Vec<_> = conversations.iter().flat_map(|(_, turns)| turns).collect();
    let drafted = || {
        ctx.publish(Progress::new(OUTLINE, "drafted a first outline"));
    };
    let outline = outlines(tools, ctx, topic, conversation(&all), drafted).await?;
    ctx.publish(Progress::new(OUTLINE, "refined the outline"));
    let planned = Article::from_outline(topic, &outline);

    let snippets = Snippets::embed(&tools.encoder, ctx, sources(&conversations)).await?;
    let mut titles = planned.sections();
    if titles.is_empty() {
        tracing::error!("No outline for {topic}. Will directly search with the topic.");
        titles = vec![topic.to_owned()];
    } else {
        titles.retain(|title| !written_by_polishing(title));
    }
    let count = titles.len() as u64;
    let detail = format!("writing {count} sections with citations");
    ctx.publish(counted(ARTICLE, detail, 0, count, Some("sections")));
    let finished = AtomicU64::new(0);
    let sections: Vec<_> = titles
        .iter()
        .map(|title| {
            let (ctx, planned, snippets, finished) = (ctx.clone(), &planned, &snippets, &finished);
            async move {
                ctx.publish(Progress::new(ARTICLE, format!("writing {title}")));
                // With no outline the title is the topic, which names the article itself.
                let queries = planned.outline(Some(title), false);
                let found = snippets.nearest(&tools.encoder, &ctx, &queries).await?;
                let written = write_section(tools, &ctx, topic, title, &found).await?;
                let done = finished.fetch_add(1, Ordering::Relaxed) + 1;
                let detail = format!("finished {title} ({done}/{count})");
                ctx.publish(counted(ARTICLE, detail, done, count, Some("sections")));
                Ok::<_, LlmError>((written, found))
            }
        })
        .collect();
    let sections: Vec<_> =
        futures::stream::iter(sections).buffered(tools.max_threads.max(1)).collect().await;
    let mut draft = planned.clone();
    for section in sections {
        let (written, found) = section?;
        draft.add_section(&written, &found);
    }
    draft.finish();
    ctx.publish(Progress::new(ARTICLE, "assembled the article"));

    let mut article = draft.clone();
    if options.do_polish_article {
        ctx.publish(Progress::new(POLISH, "polishing the article"));
        let text = draft.text();
        let lead = write_lead(tools, ctx, topic, &text).await?;
        article.insert(&parse_article(&format!("# summary\n{lead}\n\n{text}")), None, false);
        article.finish();
        ctx.publish(Progress::new(POLISH, "polished the article"));
    }
    // A polish that left nothing falls back on the draft, as Python's files did.
    let chosen = if article.text().trim().is_empty() { &draft } else { &article };
    let text = chosen.text().trim().to_owned();
    if text.is_empty() {
        return Err(StormError::NoArticle);
    }
    let mut citations: Vec<Citation> = chosen
        .numbers
        .iter()
        .map(|(url, &index)| {
            let source = chosen.sources.get(url);
            Citation {
                index,
                url: url.clone(),
                title: source.map(|s| s.title.clone()).unwrap_or_default(),
                snippet: source
                    .map(|s| s.snippets.first().unwrap_or(&s.description).clone())
                    .unwrap_or_default(),
                source: None,
            }
        })
        .collect();
    citations.sort_by_key(|citation| citation.index);
    Ok(ArticleResult {
        article: text,
        outline: planned.outline(None, true).join("\n").trim().to_owned(),
        citations,
        duration_seconds: (started.elapsed().as_secs_f64() * 10.0).round() / 10.0,
    })
}
