//! The research stage: the editors who will write, and the conversation each has with an expert
//! who searches to answer.

use std::collections::HashSet;
use std::time::Duration;

use futures::StreamExt;
use kvasir_fetch::{Web, domain};
use kvasir_llm::LlmError;
use kvasir_research::{Tools, note_avoided};
use kvasir_run::RunCtx;
use kvasir_sites::sources::DEADLINE;
use kvasir_types::sites::Tier;
use reqwest::header::{LOCATION, USER_AGENT};
use url::Url;

use crate::article::{Info, add_snippets};
use crate::signatures::{answer, ask, ask_as, personas, queries, related_topics};
use crate::text::{clean_answer, finish_sentences, limit_words, remove_citations};

/// The editor every article has, whatever the topic.
pub const BASIC_FACTS: &str = "Basic fact writer: Basic fact writer focusing on broadly covering \
the basic facts about the topic.";
/// What a writer says to end the conversation.
const THANKS: &str = "Thank you so much for your help!";
/// What the expert answers when no search found anything, so it does not make an answer up.
pub const NOTHING_FOUND: &str =
    "Sorry, I cannot find information for this question. Please ask another question.";
/// Queries one question is searched with at most.
const QUERIES_PER_TURN: usize = 3;
const MAX_REDIRECTS: usize = 5;
const CONTENTS_TIMEOUT: Duration = Duration::from_secs(30);
const NOT_CONTENTS: [&str; 5] = ["Contents", "See also", "Notes", "References", "External links"];

/// One question and its answer.
#[derive(Clone, Debug, PartialEq)]
pub struct Turn {
    pub question: String,
    pub answer: String,
    pub queries: Vec<String>,
    pub results: Vec<Info>,
}

/// The title and table of contents of the page at `url`, which a model chose. Neither it nor a
/// redirect may reach this service's network, so each hop is checked before it is asked.
pub async fn contents(web: &Web, url: &str) -> Result<(String, String), String> {
    let mut url = Url::parse(url).map_err(|_| format!("not a public URL: {url}"))?;
    for _ in 0..=MAX_REDIRECTS {
        let public = matches!(url.scheme(), "http" | "https")
            && web.public(url.host_str().unwrap_or("")).await;
        if !public {
            return Err(format!("not a public URL: {url}"));
        }
        let response = web
            .single()
            .get(url.clone())
            .header(USER_AGENT, web.user_agent())
            .timeout(CONTENTS_TIMEOUT)
            .send()
            .await
            .map_err(|err| err.to_string())?;
        let redirect = matches!(response.status().as_u16(), 301 | 302 | 303 | 307 | 308)
            .then(|| response.headers().get(LOCATION))
            .flatten()
            .and_then(|location| location.to_str().ok());
        if let Some(location) = redirect {
            url = url.join(location).map_err(|err| err.to_string())?;
            continue;
        }
        let html = response.text().await.map_err(|err| err.to_string())?;
        return page_contents(&html).ok_or_else(|| "no h1 on the page".to_owned());
    }
    Err(format!("too many redirects: {url}"))
}

/// The first `h1` of `html`, and its `h2` to `h6` headings as an indented table of contents.
pub fn page_contents(html: &str) -> Option<(String, String)> {
    let clean = |text: &str| text.replace("[edit]", "").trim().replace('\u{a0}', " ");
    let document = dom_query::Document::from(html);
    let title = document.select("h1").first();
    if !title.exists() {
        return None;
    }
    let mut toc = String::new();
    let mut levels: Vec<u32> = Vec::new();
    for header in document.select("h2, h3, h4, h5, h6").nodes() {
        let tag = header.node_name().unwrap_or_default();
        let level = tag[1..].parse().unwrap_or(2);
        let name = clean(&header.text());
        if NOT_CONTENTS.contains(&name.as_str()) {
            continue;
        }
        while levels.last().is_some_and(|&last| level <= last) {
            levels.pop();
        }
        levels.push(level);
        toc += &format!("{}{name}\n", "  ".repeat(levels.len() - 1));
    }
    Some((clean(&title.text()), toc.trim().to_owned()))
}

/// The editors who write about `topic`: the basic fact writer, then at most `max` the model
/// chooses from the contents of related pages.
pub async fn editors(
    tools: &Tools,
    web: &Web,
    ctx: &RunCtx,
    topic: &str,
    max: usize,
) -> Result<Vec<String>, LlmError> {
    let input = related_topics::Input { topic: topic.to_owned() };
    let related = tools.predict::<related_topics::Sig>(ctx, "question_asker", &input).await?;
    let mut examples = Vec::new();
    for url in related.related_topics.iter().filter(|url| url.starts_with("http")) {
        match contents(web, url).await {
            Ok((title, toc)) => examples.push(format!("Title: {title}\nTable of Contents: {toc}")),
            Err(err) => tracing::error!("Error occurs when processing {url}: {err}"),
        }
    }
    if examples.is_empty() {
        examples.push("N/A".to_owned());
    }
    let input =
        personas::Input { topic: topic.to_owned(), examples: examples.join("\n----------\n") };
    let chosen = tools.predict::<personas::Sig>(ctx, "question_asker", &input).await?.personas;
    let mut editors = vec![BASIC_FACTS.to_owned()];
    editors.extend(chosen.into_iter().take(max));
    Ok(editors)
}

/// What `queries` found, each searched once, side by side. A query that fails costs only its own
/// results, and a result from a site to avoid is dropped.
pub async fn retrieve(tools: &Tools, ctx: &RunCtx, queries: &[String]) -> Vec<Info> {
    let mut unique: Vec<&String> = Vec::new();
    for query in queries {
        if !unique.contains(&query) {
            unique.push(query);
        }
    }
    // Collected first: a lazy map left in the stream keeps the future from being Send.
    let searches: Vec<_> = unique.iter().map(|query| search(tools, ctx, query)).collect();
    let found: Vec<Vec<Info>> =
        futures::stream::iter(searches).buffered(tools.max_threads.max(1)).collect().await;
    ctx.run.record_search(unique.len() as u64);
    found.concat()
}

async fn search(tools: &Tools, ctx: &RunCtx, query: &str) -> Vec<Info> {
    let (mut hits, _failures) = tools.search.search(query, &HashSet::new(), ctx).await;
    if let Some(sources) = &tools.sources {
        let hosts = hits.iter().map(|hit| domain(&hit.url)).collect();
        let tiers = sources.tiers(hosts, DEADLINE).await;
        let avoided = hits
            .iter()
            .map(|hit| domain(&hit.url))
            .filter(|host| tiers.get(host) == Some(&Tier::Avoid))
            .collect();
        note_avoided(sources.as_ref(), ctx, avoided).await;
        hits.retain(|hit| tiers.get(&domain(&hit.url)) != Some(&Tier::Avoid));
    }
    hits.into_iter()
        .map(|hit| Info {
            // STORM writes with citations and follows none a source makes.
            snippets: hit.snippets.iter().map(|snippet| remove_citations(snippet)).collect(),
            url: hit.url,
            title: hit.title,
            description: hit.description,
        })
        .collect()
}

/// The writer's view of the conversation: the last four turns in full, earlier questions only.
fn history(turns: &[Turn]) -> String {
    let recent = turns.len().saturating_sub(4);
    let mut lines: Vec<String> = turns[..recent]
        .iter()
        .map(|turn| {
            format!("You: {}\nExpert: Omit the answer here due to space limit.", turn.question)
        })
        .collect();
    lines.extend(
        turns[recent..].iter().map(|turn| {
            format!("You: {}\nExpert: {}", turn.question, remove_citations(&turn.answer))
        }),
    );
    let joined = lines.join("\n");
    let conv = match joined.trim() {
        "" => "N/A",
        conv => conv,
    };
    limit_words(conv, 2500)
}

/// The expert's answer to `question`, or None when the model failed to give one.
async fn expert(
    tools: &Tools,
    ctx: &RunCtx,
    topic: &str,
    question: &str,
) -> Result<(Vec<String>, Vec<Info>, Option<String>), LlmError> {
    let input = queries::Input { topic: topic.to_owned(), question: question.to_owned() };
    let mut queries = tools.predict::<queries::Sig>(ctx, "conv_simulator", &input).await?.queries;
    queries.truncate(QUERIES_PER_TURN);
    let results = retrieve(tools, ctx, &queries).await;
    if results.is_empty() {
        return Ok((queries, results, Some(NOTHING_FOUND.to_owned())));
    }
    let mut info = String::new();
    for (n, result) in results.iter().enumerate() {
        if let Some(snippet) = result.snippets.first() {
            info += &format!("[{}]: {snippet}", n + 1);
        }
        info += "\n\n";
    }
    let input = answer::Input {
        topic: topic.to_owned(),
        conv: question.to_owned(),
        info: limit_words(&info, 1000),
    };
    let answer = match tools.predict::<answer::Sig>(ctx, "conv_simulator", &input).await {
        Ok(output) => Some(finish_sentences(&output.answer)),
        Err(err) => {
            tracing::error!("Failed to answer {question:?}: {err}");
            None
        }
    };
    Ok((queries, results, answer))
}

/// The conversation `editor` has about `topic`, of at most `max_turns` turns, `turned` called
/// after each. Its answers come cleaned of references and of citations past their sources.
pub async fn converse(
    tools: &Tools,
    ctx: &RunCtx,
    topic: &str,
    editor: &str,
    max_turns: usize,
    turned: &(dyn Fn(&RunCtx) + Send + Sync),
) -> Result<Vec<Turn>, LlmError> {
    let mut turns: Vec<Turn> = Vec::new();
    for _ in 0..max_turns {
        let conv = history(&turns);
        let question = if editor.trim().is_empty() {
            let input = ask::Input { topic: topic.to_owned(), conv };
            tools.predict::<ask::Sig>(ctx, "question_asker", &input).await?.question
        } else {
            let input = ask_as::Input { topic: topic.to_owned(), persona: editor.to_owned(), conv };
            tools.predict::<ask_as::Sig>(ctx, "question_asker", &input).await?.question
        };
        if question.is_empty() {
            tracing::error!("Simulated Wikipedia writer utterance is empty.");
            break;
        }
        if question.starts_with(THANKS) {
            break;
        }
        let (queries, results, answer) = expert(tools, ctx, topic, &question).await?;
        // Dropped rather than recorded with a canned apology. Recording nothing means the writer
        // would ask the same question again, so the conversation ends here with what it has.
        let Some(answer) = answer else {
            tracing::error!("Ending the conversation after a failed answer.");
            break;
        };
        turns.push(Turn { question, answer, queries, results });
        turned(ctx);
    }
    for turn in &mut turns {
        turn.answer = clean_answer(&turn.answer, turn.results.len());
    }
    Ok(turns)
}

/// Every source the conversations found, by URL, its snippets merged.
pub fn sources(conversations: &[(String, Vec<Turn>)]) -> Vec<Info> {
    let mut found: Vec<Info> = Vec::new();
    for turn in conversations.iter().flat_map(|(_, turns)| turns) {
        for result in &turn.results {
            match found.iter_mut().find(|info| info.url == result.url) {
                Some(info) => add_snippets(&mut info.snippets, &result.snippets),
                None => {
                    let mut info = result.clone();
                    info.snippets.clear();
                    add_snippets(&mut info.snippets, &result.snippets);
                    found.push(info);
                }
            }
        }
    }
    found
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_page_gives_its_title_and_contents() {
        let html = "<h1>Rok\u{a0}stone[edit]</h1><h2>History</h2><h3>Finding</h3><h2>Notes</h2>\
                    <h2>Runes</h2>";
        let (title, toc) = page_contents(html).unwrap();
        assert_eq!(title, "Rok stone");
        assert_eq!(toc, "History\n  Finding\nRunes");
        assert_eq!(page_contents("<h2>x</h2>"), None);
    }

    #[test]
    fn the_writer_sees_the_last_four_answers() {
        let turn = |n: usize| Turn {
            question: format!("q{n}"),
            answer: format!("a{n} [1]"),
            queries: Vec::new(),
            results: Vec::new(),
        };
        assert_eq!(history(&[]), "N/A");
        let turns: Vec<Turn> = (1..=5).map(turn).collect();
        let seen = history(&turns);
        assert!(seen.starts_with(
            "You: q1\nExpert: Omit the answer here due to space limit.\nYou: q2\nExpert: a2"
        ));
        assert!(!seen.contains("[1]"));
    }
}
