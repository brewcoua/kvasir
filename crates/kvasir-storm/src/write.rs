//! The writing stages: the outline from the conversations, a section for each of its headings from
//! the snippets nearest it, and the lead that polishing adds.

use kvasir_llm::LlmError;
use kvasir_research::encoder::cosine;
use kvasir_research::{Encoder, Tools};
use kvasir_run::RunCtx;

use crate::article::{Info, add_snippets};
use crate::curation::Turn;
use crate::signatures::{draft_outline, lead, outline, section};
use crate::text::{clean_outline, clean_section, limit_words, remove_citations};

/// Snippets picked for each query of a section.
pub const SNIPPETS_PER_QUERY: usize = 3;

/// The conversations as the outline is told them. A turn about "the topic you" want is the writer
/// talking about the task rather than the topic, and is left out.
pub fn conversation(turns: &[&Turn]) -> String {
    let told: Vec<String> = turns
        .iter()
        .filter(|turn| {
            !turn.answer.to_lowercase().contains("topic you")
                && !turn.question.to_lowercase().contains("topic you")
        })
        .map(|turn| format!("Wikipedia Writer: {}\nExpert: {}", turn.question, turn.answer))
        .collect();
    limit_words(&remove_citations(&told.join("\n")), 5000)
}

/// A first outline from what the model knows, then one improved from the conversation.
pub async fn outlines(
    tools: &Tools,
    ctx: &RunCtx,
    topic: &str,
    conv: String,
    drafted: impl Fn(),
) -> Result<String, LlmError> {
    let input = draft_outline::Input { topic: topic.to_owned() };
    let draft = tools.predict::<draft_outline::Sig>(ctx, "outline_gen", &input).await?.outline;
    let old_outline = clean_outline(&draft);
    drafted();
    let input = outline::Input { topic: topic.to_owned(), conv, old_outline };
    let improved = tools.predict::<outline::Sig>(ctx, "outline_gen", &input).await?.outline;
    Ok(clean_outline(&improved))
}

/// Every snippet found, embedded, for picking those nearest a section.
pub struct Snippets {
    found: Vec<(usize, String)>,
    vectors: Vec<Vec<f32>>,
    sources: Vec<Info>,
}

impl Snippets {
    pub async fn embed(
        encoder: &Encoder,
        ctx: &RunCtx,
        sources: Vec<Info>,
    ) -> Result<Self, LlmError> {
        let found: Vec<(usize, String)> = sources
            .iter()
            .enumerate()
            .flat_map(|(i, info)| info.snippets.iter().map(move |snippet| (i, snippet.clone())))
            .collect();
        let texts: Vec<String> = found.iter().map(|(_, snippet)| snippet.clone()).collect();
        let vectors = encoder.encode(&texts, false, ctx).await?;
        Ok(Self { found, vectors, sources })
    }

    /// The sources of the snippets nearest each of `queries`, each with only those snippets, in
    /// the order first picked. Nothing found gives nothing to pick from.
    pub async fn nearest(
        &self,
        encoder: &Encoder,
        ctx: &RunCtx,
        queries: &[String],
    ) -> Result<Vec<Info>, LlmError> {
        if self.found.is_empty() {
            return Ok(Vec::new());
        }
        let asked = encoder.encode(queries, false, ctx).await?;
        let mut picked: Vec<Info> = Vec::new();
        for query in &asked {
            let mut order: Vec<(f64, usize)> =
                self.vectors.iter().map(|v| cosine(query, v)).zip(0..).collect();
            order.sort_by(|a, b| b.0.total_cmp(&a.0));
            for &(_, at) in order.iter().take(SNIPPETS_PER_QUERY) {
                let (source, snippet) = &self.found[at];
                let source = &self.sources[*source];
                match picked.iter_mut().find(|info| info.url == source.url) {
                    Some(info) => add_snippets(&mut info.snippets, std::slice::from_ref(snippet)),
                    None => picked.push(Info { snippets: vec![snippet.clone()], ..source.clone() }),
                }
            }
        }
        Ok(picked)
    }
}

/// The section `title` of an article on `topic`, written from `sources`, numbered from 1.
pub async fn write_section(
    tools: &Tools,
    ctx: &RunCtx,
    topic: &str,
    title: &str,
    sources: &[Info],
) -> Result<String, LlmError> {
    let mut info = String::new();
    for (i, source) in sources.iter().enumerate() {
        info += &format!("[{}]\n{}\n\n", i + 1, source.snippets.join("\n"));
    }
    let input = section::Input {
        info: limit_words(&info, 1500),
        topic: topic.to_owned(),
        section: title.to_owned(),
    };
    let written = tools.predict::<section::Sig>(ctx, "article_gen", &input).await?;
    Ok(clean_section(&written.written_section))
}

/// Whether the polishing stage writes this section instead: an introduction or a conclusion.
pub fn written_by_polishing(title: &str) -> bool {
    let title = title.trim().to_lowercase();
    title == "introduction" || title.starts_with("conclusion") || title.starts_with("summary")
}

/// The lead of the article `draft` is the text of.
pub async fn write_lead(
    tools: &Tools,
    ctx: &RunCtx,
    topic: &str,
    draft: &str,
) -> Result<String, LlmError> {
    let input = lead::Input { topic: topic.to_owned(), draft_page: draft.to_owned() };
    Ok(tools.predict::<lead::Sig>(ctx, "article_gen", &input).await?.lead_section)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn turns_about_the_task_are_left_out_of_the_conversation() {
        let turn = |question: &str, answer: &str| Turn {
            question: question.into(),
            answer: answer.into(),
            queries: Vec::new(),
            results: Vec::new(),
        };
        let turns = [turn("When [1]?", "In 1783 [1]."), turn("Which topic you mean?", "x")];
        let refs: Vec<&Turn> = turns.iter().collect();
        assert_eq!(conversation(&refs), "Wikipedia Writer: When ?\nExpert: In 1783 .");
    }

    #[test]
    fn an_introduction_or_conclusion_is_left_to_polishing() {
        assert!(written_by_polishing(" Introduction "));
        assert!(written_by_polishing("Conclusions"));
        assert!(!written_by_polishing("History"));
    }
}
