//! STORM's signatures, upstream's prompts. Those that were a `dspy.ChainOfThought` reason first.
//!
//! `PolishPage` is not here: it ran only with `remove_duplicate`, which nothing ever set.

use kvasir_llm::signature;

signature!(related_topics, "kvasir.storm.storm_wiki.modules.persona_generator.FindRelatedTopic",
    include_str!("prompts/related_topics.md"),
    in { topic: String = "topic of interest" },
    out { reasoning: String = "",
          related_topics: Vec<String> = "URLs of Wikipedia pages on closely related subjects" });

signature!(personas, "kvasir.storm.storm_wiki.modules.persona_generator.GenPersona",
    include_str!("prompts/personas.md"),
    in { topic: String = "topic of interest",
         examples: String = "wiki page outlines of related topics, for inspiration" },
    out { reasoning: String = "",
          personas: Vec<String> = "one editor per item, written as 'short summary of the editor: description'" });

signature!(ask, "kvasir.storm.storm_wiki.modules.knowledge_curation.AskQuestion",
    include_str!("prompts/ask.md"),
    in { topic: String = "topic you want to write about",
         conv: String = "conversation history" },
    out { reasoning: String = "",
          question: String = "a single question, or the closing thanks" });

signature!(ask_as, "kvasir.storm.storm_wiki.modules.knowledge_curation.AskQuestionWithPersona",
    include_str!("prompts/ask_as.md"),
    in { topic: String = "topic you want to write about",
         persona: String = "your persona besides being a Wikipedia writer",
         conv: String = "conversation history" },
    out { reasoning: String = "",
          question: String = "a single question, or the closing thanks" });

signature!(queries, "kvasir.storm.storm_wiki.modules.knowledge_curation.QuestionToQuery",
    include_str!("prompts/queries.md"),
    in { topic: String = "topic you are discussing about",
         question: String = "question you want to answer" },
    out { queries: Vec<String> = "the search queries you will use" });

signature!(answer, "kvasir.storm.storm_wiki.modules.knowledge_curation.AnswerQuestion",
    include_str!("prompts/answer.md"),
    in { topic: String = "topic you are discussing about",
         conv: String = "the question",
         info: String = "gathered information" },
    out { answer: String = "" });

signature!(draft_outline, "kvasir.storm.storm_wiki.modules.outline_generation.WritePageOutline",
    include_str!("prompts/draft_outline.md"),
    in { topic: String = "the topic you want to write about" },
    out { outline: String = "the Wikipedia page outline" });

signature!(outline, "kvasir.storm.storm_wiki.modules.outline_generation.WritePageOutlineFromConv",
    include_str!("prompts/outline.md"),
    in { topic: String = "the topic you want to write about",
         conv: String = "conversation history",
         old_outline: String = "the current outline, to be improved" },
    out { outline: String = "the improved Wikipedia page outline" });

signature!(section, "kvasir.storm.storm_wiki.modules.article_generation.WriteSection",
    include_str!("prompts/section.md"),
    in { info: String = "the collected information",
         topic: String = "the topic of the page",
         section: String = "the section you need to write" },
    out { written_section: String = "the section, with proper inline citations. Start with the # section title. Do not include the page title or write other sections." });

signature!(lead, "kvasir.storm.storm_wiki.modules.article_polish.WriteLeadSection",
    include_str!("prompts/lead.md"),
    in { topic: String = "the topic of the page",
         draft_page: String = "the draft page" },
    out { lead_section: String = "" });
