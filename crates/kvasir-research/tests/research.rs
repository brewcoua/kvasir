//! Reading pages into claims, recalling what was read, writing, rendering and the text checks.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::{Fetched, at, claim, ctx, events, page, target, tools};
use kvasir_extract::{PIECE_CHARACTERS, split};
use kvasir_fetch::Page;
use kvasir_research::claims::{Claim, Confidence};
use kvasir_research::clarify::clarify;
use kvasir_research::corpus::{Corpus, RECALL_MIN, RECALL_PER_SITE, RECALL_PIECES};
use kvasir_research::read::{self, GAP, PAGE_CHARACTERS, pack, page_characters, relevant_pieces};
use kvasir_research::render::{matrix, render};
use kvasir_research::style::clean;
use kvasir_research::text::{misleading, names, quoted, with_table_header};
use kvasir_research::verify;
use kvasir_research::write;
use kvasir_research::{Hit, Tools};
use kvasir_run::RunCtx;
use kvasir_testkit::{ScriptedLlm, reply};
use kvasir_types::results::{Cell, Criterion, Row};

const NONSENSE: &str = "[[ ## nonsense ## ]]\nx\n\n[[ ## completed ## ]]";

fn owned(sections: &[(&str, &str)]) -> Vec<(String, String)> {
    sections.iter().map(|(t, s)| ((*t).to_owned(), (*s).to_owned())).collect()
}

fn by_id(claims: &[Claim]) -> HashMap<u64, Claim> {
    claims.iter().map(|claim| (claim.id, claim.clone())).collect()
}

#[test]
fn render_numbers_sources_by_page_and_drops_made_up_ids() {
    let claims = by_id(&[claim(1, "https://a"), claim(2, "https://b"), claim(3, "https://a")]);
    let sections = owned(&[("Findings", "First [2]. Second [1][3]. Third [3, 2]. Invented [9].")]);

    let (report, citations) = render(&sections, &claims, "");

    assert_eq!(report, "## Findings\n\nFirst [1]. Second [2]. Third [2][1]. Invented .");
    let numbered: Vec<(u64, &str)> = citations.iter().map(|c| (c.index, c.url.as_str())).collect();
    assert_eq!(numbered, [(1, "https://b"), (2, "https://a")]);
}

#[test]
fn render_drops_a_heading_that_repeats_the_section_title() {
    let sections = owned(&[("Key Findings", "### Key Findings\n\nText.\n\n### Detail\n\nMore.")]);

    let (report, _) = render(&sections, &HashMap::new(), "");

    assert_eq!(report, "## Key Findings\n\nText.\n\n### Detail\n\nMore.");
}

#[test]
fn a_quote_matches_despite_punctuation_and_ellipses() {
    let page = "Grouped Query Attention , a context window of 16,384 tokens with a sliding window \
                attention, and was trained using the Fill-in-the-Middle objective.\n\
                13B, quantised to 4 bits, llama-2-13b.bin RAM allocation 7.1 GB\n\
                - llama-2-13b-chat.ggmlv3.q4_0.bin (43/43 layers to GPU): 27.63 tokens per second\n\
                - llama-2-13b-chat.ggmlv3.q8_0.bin (43/43 layers to GPU): 19.94 tokens per second";
    // Each one a quote gpt-oss wrote that the page supports, from a live run.
    for quote in [
        "trained using the Fill\u{2011}in\u{2011}the\u{2011}Middle objective",
        "a context window of 16,384 tokens with a sliding window",
        "43/43 layers to GPU): 27.63 tokens per second\nllama-2-13b-chat.ggmlv3.q8_0.bin",
        "13B, quantised ... RAM allocation 7.1 GB",
    ] {
        assert!(quoted(quote, page), "{quote}");
    }
}

#[test]
fn a_quote_not_on_the_page_is_refused() {
    let page =
        "13B, quantised to 4 bits, RAM allocation 7.1 GB. A context window of 16,384 tokens.";
    for quote in [
        "a context window of 32,768 tokens",
        // Fragments out of order.
        "RAM allocation 7.1 GB ... 13B, quantised",
        // A word that only appears inside a longer one.
        "4 bit",
        "",
        "...",
    ] {
        assert!(!quoted(quote, page), "{quote}");
    }
}

#[test]
fn a_quote_right_after_a_negation_is_misleading() {
    for page in
        ["It never grows in the dark.", "It isn't grows in the dark.", "Unless grows in the dark."]
    {
        assert!(quoted("grows in the dark", page));
        assert!(misleading("grows in the dark", page), "{page}");
    }
}

#[test]
fn an_ellipsis_over_a_negator_is_misleading() {
    assert!(misleading("the drug ... works", "the drug does not work works"));
    assert!(misleading("it ... grows", "it is only said to grows"));
    assert!(misleading("this ... helps", "this isn't known to helps"));
}

#[test]
fn an_ordinary_quote_is_not_misleading() {
    for (quote, page) in [
        ("grows in the dark", "It grows in the dark."),
        ("the drug ... works", "The drug often really works."),
        // A "t" that is not a contraction.
        ("cells grow", "T cells grow."),
        ("cells divide", "Seen in T cells divide."),
        // The negation is after the quote, or in another sentence's end.
        ("the drug works", "The drug works, not always."),
        ("cats grow", "It is not. Cats grow."),
    ] {
        assert!(quoted(quote, page), "{quote}");
        assert!(!misleading(quote, page), "{quote}");
    }
}

fn findings(found: &[(&str, &str, &str)]) -> String {
    let found: Vec<serde_json::Value> = found
        .iter()
        .map(|(claim, quote, confidence)| {
            serde_json::json!({"claim": claim, "quote": quote, "confidence": confidence})
        })
        .collect();
    reply(&[("findings", &serde_json::to_string(&found).unwrap())])
}

const FOUND: (&str, &str, &str) = ("Found", "found in 1904", "high");

fn hit(url: &str, title: &str, description: &str) -> Hit {
    Hit { url: url.into(), title: title.into(), description: description.into(), ..Hit::default() }
}

/// The claims intake reads from `hits`, each page one of `pages` or failing, with the intake model
/// answering `replies`.
async fn read_with(
    pages: HashMap<String, Page>,
    hits: &[Hit],
    replies: &[&str],
) -> (Vec<Claim>, Arc<ScriptedLlm>) {
    let llm = Arc::new(ScriptedLlm::by_role(&[("intake", replies)]));
    let mut tools = tools(&llm, &["intake"]);
    tools.pages = Arc::new(Fetched { pages, any: None });
    let mut corpus = Corpus::default();
    let stage = kvasir_run::stages::RESEARCH;
    let claims =
        read::read(&tools, &ctx(), &mut corpus, "b", "q", hits, 1, 1, "search 1", None, stage)
            .await
            .unwrap();
    (claims, llm)
}

/// The claims intake reads from one page holding `text`.
async fn intake(text: &str, replies: &[&str]) -> Vec<Claim> {
    let pages = HashMap::from([("https://a".to_owned(), page(text))]);
    read_with(pages, &[hit("https://a", "A", "")], replies).await.0
}

fn texts(claims: &[Claim]) -> Vec<&str> {
    claims.iter().map(|claim| claim.text.as_str()).collect()
}

#[tokio::test]
async fn a_page_that_cannot_be_read_falls_back_to_its_snippet() {
    let pages = HashMap::from([("https://up".to_owned(), page("It was found in 1904."))]);
    let hits = [
        hit("https://down", "D", "It was found in 1904, says the snippet."),
        hit("https://up", "U", "the snippet"),
    ];
    let found = findings(&[FOUND]);

    let (claims, llm) = read_with(pages, &hits, &[&found, &found]).await;

    let read: Vec<(&str, bool)> = claims.iter().map(|c| (c.url.as_str(), c.from_snippet)).collect();
    assert_eq!(read, [("https://down", true), ("https://up", false)]);
    let sent: Vec<String> = llm.sent().into_iter().map(|s| s.messages[1].content.clone()).collect();
    assert!(sent[0].contains("says the snippet") && sent[1].contains("It was found in 1904."));
}

#[tokio::test]
async fn a_lead_that_cannot_be_read_gives_no_text() {
    let lead = Hit { depth: Some(1), ..hit("https://down", "D", "what another page says") };

    let (claims, llm) = read_with(HashMap::new(), &[lead], &[]).await;

    assert!(claims.is_empty());
    assert!(llm.sent().is_empty());
}

#[tokio::test]
async fn intake_drops_a_quote_a_negation_turns_around() {
    let found = findings(&[
        ("Grows", "grows in the dark", "high"),
        ("Drug works", "the drug ... works", "high"),
        FOUND,
    ]);
    let piece = "It never grows in the dark. The drug is not known to works. It was found in 1904.";

    assert_eq!(texts(&intake(piece, &[&found]).await), ["Found"]);
}

const TABLE: &str = "Benchmarks\n\n| Metric | WasmEdge | Wasmtime | Wasmer |\n|---|---|---|---|\n\
                     | Cold start | 1.5ms | 3ms | 2ms |\n| Memory | 9MB | 12MB | 11MB |\n\nAfter the table.";

#[test]
fn a_table_row_quote_gets_its_header() {
    let quote = with_table_header("Cold start 1.5ms 3ms 2ms", TABLE);

    assert_eq!(quote, "| Metric | WasmEdge | Wasmtime | Wasmer | ... Cold start 1.5ms 3ms 2ms");
    assert!(quoted(&quote, TABLE));
}

#[test]
fn a_quote_outside_a_table_or_with_its_header_is_unchanged() {
    let with_header = "Metric WasmEdge Wasmtime Wasmer";

    assert_eq!(with_table_header("After the table", TABLE), "After the table");
    assert_eq!(with_table_header(with_header, TABLE), with_header);
}

#[tokio::test]
async fn intake_stores_a_table_row_with_its_header() {
    let found = findings(&[("Cold start", "Cold start 1.5ms 3ms 2ms", "high")]);

    let claims = intake(TABLE, &[&found]).await;

    assert_eq!(claims.len(), 1);
    assert!(claims[0].quote.starts_with("| Metric | WasmEdge"));
}

#[tokio::test]
async fn unreadable_claims_cost_the_piece_not_the_run() {
    assert!(intake("text", &[NONSENSE; 2]).await.is_empty());
}

#[tokio::test]
async fn a_confidence_of_moderate_is_medium() {
    let found = findings(&[("Found", "found in 1904", "moderate")]);

    let claims = intake("It was found in 1904.", &[&found]).await;

    assert_eq!(claims.iter().map(|c| c.confidence).collect::<Vec<_>>(), [Confidence::Medium]);
}

#[tokio::test]
async fn a_finding_that_fails_validation_costs_the_piece_not_the_run() {
    let found = findings(&[("Found", "found in 1904", "certain")]);

    assert!(intake("It was found in 1904.", &[&found, &found]).await.is_empty());
}

#[tokio::test]
async fn an_unreadable_section_is_empty_rather_than_the_end_of_the_run() {
    let llm = Arc::new(ScriptedLlm::by_role(&[("write", &[NONSENSE; 2])]));
    let tools = tools(&llm, &["write"]);
    let titles = ["A".to_owned()];

    let text = write::section(
        &tools,
        &ctx(),
        "brief",
        &titles,
        "A",
        &[],
        &[claim(1, "https://a")],
        &[],
        &[],
    )
    .await
    .unwrap();

    assert_eq!(text, "");
}

#[tokio::test]
async fn a_section_is_shown_the_closest_claims_and_the_extra_ones() {
    let text = reply(&[("text", "Notably, it was found [1].")]);
    // Claim 20 is the farthest from the section, so only the extra ids bring it in.
    let llm = Arc::new(
        ScriptedLlm::by_role(&[("write", &[&text])])
            .with_vectors(&[("claim 20", &[0.0, 1.0])], Some(&[1.0, 0.0])),
    );
    let tools = tools(&llm, &["write"]);
    let claims: Vec<Claim> = (1..=20).map(|id| claim(id, "https://a")).collect();
    let earlier = owned(&[("Before", &"x".repeat(500))]);
    let titles = ["Before".to_owned(), "A".to_owned()];

    let written =
        write::section(&tools, &ctx(), "b", &titles, "A", &earlier, &claims, &[20], &[]).await;

    assert_eq!(written.unwrap(), "It was found [1].");
    let prompt = &llm.sent()[0].messages[1].content;
    assert!(prompt.contains(&format!("Before: {}\n", "x".repeat(400))));
    assert_eq!(prompt.matches("] claim ").count(), write::CLAIMS_PER_SECTION + 1);
    assert!(prompt.contains("[20] claim 20"));
}

#[tokio::test]
async fn the_summary_is_a_cleaned_list_of_its_points() {
    let points = reply(&[("points", r#"["Notably,  rain failed [1].", " ", "Ships sank [2]."]"#)]);
    let llm = Arc::new(ScriptedLlm::by_role(&[("tldr", &[&points])]));
    let tools = tools(&llm, &["tldr"]);

    let summary = write::summary(&tools, &ctx(), "b", &owned(&[("A", "a"), ("B", "b")])).await;

    assert_eq!(summary.unwrap(), "- Rain failed [1].\n- Ships sank [2].");
    assert!(llm.sent()[0].messages[1].content.contains("## A\na\n\n## B\nb"));
}

#[tokio::test]
async fn clarify_asks_at_most_three_questions() {
    let questions = reply(&[("questions", r#"["a?", "b?", "c?", "d?"]"#)]);
    let llm = ScriptedLlm::replying(&[&questions]);

    let asked = clarify(&llm, &target("clarify"), "best battery makers").await.unwrap();

    assert_eq!(asked, ["a?", "b?", "c?"]);
}

#[test]
fn a_sentence_keeps_the_markers_after_its_full_stop() {
    let line = "Qwen fits in 24 GB [1]. It runs at 30 tok/s, e.g. on a 3090. [2][3] Unsure.";

    assert_eq!(
        verify::sentences(line),
        ["Qwen fits in 24 GB [1].", "It runs at 30 tok/s, e.g. on a 3090. [2][3]", "Unsure."]
    );
}

#[test]
fn editing_decides_only_cited_sentences() {
    let text = [
        "Intro without citation.",
        "Supported [1]. Unsupported [2]. Invented [9].",
        "- Only unsupported [2].",
        "- Kept item [1].",
        "| Model | VRAM [2] |",
        "### Heading [2]",
    ]
    .join("\n");
    let mut asked = Vec::new();

    let kept = verify::edit(&text, |sentence| {
        asked.push(sentence.to_owned());
        let dropped = ["Unsupported", "Invented", "Only"].iter().any(|s| sentence.starts_with(s));
        if dropped { String::new() } else { sentence.to_owned() }
    });

    let expected = [
        "Intro without citation.",
        "Supported [1].",
        "- Kept item [1].",
        "| Model | VRAM [2] |",
        "### Heading [2]",
    ];
    assert_eq!(kept, expected.join("\n"));
    assert_eq!(
        asked,
        [
            "Supported [1].",
            "Unsupported [2].",
            "Invented [9].",
            "Only unsupported [2].",
            "Kept item [1]."
        ]
    );
}

#[test]
fn a_callout_keeps_its_supported_sentences_and_goes_once_it_has_none() {
    let text =
        "> [!WARNING] Disputed\n> **Dates** differ [1].\n\n> [!NOTE]\n> Wrong [1].\n\nAfter [1].";

    let kept = verify::edit(text, |sentence| {
        if sentence.starts_with("Wrong") { String::new() } else { sentence.to_owned() }
    });

    assert_eq!(kept, "> [!WARNING] Disputed\n> **Dates** differ [1].\n\n\nAfter [1].");
}

#[test]
fn a_table_has_no_rank_and_keeps_its_cells_whole() {
    let cell = Cell {
        criterion: "licence".into(),
        finding: "MIT\nor Apache".into(),
        claims: vec![4, 5],
        rank: None,
    };
    let rows = [Row { entity: "A | B".into(), rank: None, cells: vec![cell] }];

    assert_eq!(
        matrix(&rows, &[Criterion { name: "licence".into(), weight: 1 }]),
        "| Option | licence |\n| --- | --- |\n| A / B | MIT or Apache [4][5] |\n\n\
         A dash means no claim spoke to it."
    );
}

#[test]
fn a_cited_claim_must_name_the_option() {
    for (entity, text, named) in [
        ("Qwen3-Coder-30B-A3B", "Qwen3 Coder 30B runs at 69 tok/s", true),
        ("Claude Opus 5.5", "Qwen3-Coder needs 17 GB at 4-bit", false),
        // A word inside a longer one is not the name.
        ("Phi 4", "Phi 14B fits in 12 GB", false),
    ] {
        assert_eq!(names(entity, text), named, "{entity}");
    }
}

#[test]
fn a_call_reads_what_the_context_holds_once_the_prompt_and_answer_are_out() {
    // (16384 - 1000 prompt - 4096 answer) tokens, at 3.5 characters each, less a tenth.
    assert_eq!(page_characters(16384), 35557);
    assert!(page_characters(32768) > 2 * page_characters(16384));
}

#[test]
fn pieces_are_packed_in_order_into_texts_that_fit() {
    let pieces = ["aaaa", "bb", "cccccc", "d"].map(str::to_owned);
    assert_eq!(pack(&pieces, 6 + GAP.len() + 1), [format!("aaaa{GAP}bb"), format!("cccccc{GAP}d")]);
    // Too long to share, a piece is read alone.
    assert_eq!(pack(&["a".repeat(20), "b".into()], 10), ["a".repeat(20), "b".into()]);
}

#[test]
fn a_report_opens_with_its_summary_then_contents_linking_each_heading() {
    let claims = by_id(&[claim(1, "https://a"), claim(2, "https://b")]);
    let sections = owned(&[
        ("Trade & war", "### Ships\n\nShips sank [2].\n\n### Ports [2]\n\nPorts burned."),
        ("Trade & war", "Again."),
        ("Drought", "Rain failed [1]."),
    ]);

    let (report, citations) = render(&sections, &claims, "- Rain failed [1].\n- Ships sank [2].");

    assert_eq!(
        report.split("\n\n## ").next().unwrap(),
        "> [!SUMMARY] TL;DR\n> - Rain failed [1].\n> - Ships sank [2].\n\n\
         > [!ABSTRACT] Contents\n\
         > - [Trade & war](#trade--war)\n\
         >   - [Ships](#ships)\n\
         >   - [Ports](#ports)\n\
         > - [Trade & war](#trade--war-1)\n\
         > - [Drought](#drought)"
    );
    // Sources are numbered in reading order, the summary first.
    let urls: Vec<&str> = citations.iter().map(|c| c.url.as_str()).collect();
    assert_eq!(urls, ["https://a", "https://b"]);
}

#[test]
fn a_short_report_has_no_contents() {
    let (report, _) = render(&owned(&[("A", "a"), ("B", "b")]), &HashMap::new(), "");

    assert_eq!(report, "## A\n\na\n\n## B\n\nb");
}

#[test]
fn the_style_pass_takes_out_filler_but_not_quotes_or_code() {
    let text = "Notably, **Ugarit** fell 1190—1185 BC [1]. It is important to note that trade — \
                the lifeline — was really very fragile. He wrote \"very — late\" and `a — b`.";

    assert_eq!(
        clean(text),
        "**Ugarit** fell 1190–1185 BC [1]. Trade, the lifeline, was fragile. \
         He wrote \"very — late\" and `a — b`."
    );
}

fn encoding(vectors: &[(&str, &[f32])], default: Option<&[f32]>) -> (Arc<ScriptedLlm>, Tools) {
    let llm = Arc::new(ScriptedLlm::replying(&[]).with_vectors(vectors, default));
    let tools = tools(&llm, &[]);
    (llm, tools)
}

/// A page of four sections, each its own piece, of which three fit the budget.
fn long_page() -> String {
    let size = PAGE_CHARACTERS / 3 - 100;
    let sections: Vec<String> = ["Alpha", "Beta", "Gamma", "Delta"]
        .iter()
        .map(|name| format!("## {name}\n{}", name[..1].repeat(size)))
        .collect();
    format!("# Page\n{}", sections.join("\n\n"))
}

async fn relevant(tools: &Tools, corpus: &mut Corpus, text: &str) -> Vec<String> {
    relevant_pieces(tools, &ctx(), corpus, "q", "T", page(text), 1).await.unwrap()
}

#[tokio::test]
async fn the_closest_pieces_are_kept_up_to_the_page_budget_in_page_order() {
    let text = format!("{}\n\n## Coda\nshort", long_page());
    let pieces = split(&text, PIECE_CHARACTERS);
    let titled: Vec<(String, [f32; 2])> = pieces
        .iter()
        .map(|piece| {
            let far = piece.starts_with("## Beta");
            (format!("T\n{piece}"), if far { [0.0, 1.0] } else { [1.0, 0.0] })
        })
        .collect();
    let mut vectors: Vec<(&str, &[f32])> =
        titled.iter().map(|(t, v)| (t.as_str(), &v[..])).collect();
    vectors.push(("q", &[1.0, 0.0]));
    let (_, tools) = encoding(&vectors, Some(&[0.5, 0.5]));
    let mut corpus = Corpus::default();

    let kept = relevant(&tools, &mut corpus, &text).await;

    assert!(kept.iter().map(|piece| piece.chars().count()).sum::<usize>() <= PAGE_CHARACTERS);
    assert!(!kept.iter().any(|piece| piece.starts_with("## Beta")));
    let in_order: Vec<String> = pieces.iter().filter(|p| kept.contains(p)).cloned().collect();
    assert_eq!(kept, in_order);
    // The corpus holds every piece, and only the kept ones are marked read.
    assert_eq!(corpus.pieces.iter().map(|p| p.text.clone()).collect::<Vec<_>>(), pieces);
    let read: Vec<String> =
        corpus.pieces.iter().filter(|p| p.read).map(|p| p.text.clone()).collect();
    assert_eq!(read, kept);
}

#[tokio::test]
async fn a_page_within_the_budget_is_read_whole() {
    let text = "# Page\n## One\nfirst\n\n## Two\nsecond";
    let (_, tools) = encoding(&[], Some(&[0.0]));
    let mut corpus = Corpus::default();

    assert_eq!(relevant(&tools, &mut corpus, text).await, split(text, PIECE_CHARACTERS));
    assert!(corpus.pieces.iter().all(|p| p.read));
}

#[tokio::test]
async fn a_page_is_encoded_once_however_it_is_picked() {
    let text = long_page();
    let (_, tools) = encoding(&[], Some(&[1.0, 0.0]));
    let ctx = ctx();

    relevant_pieces(&tools, &ctx, &mut Corpus::default(), "q", "T", page(&text), 1).await.unwrap();

    // One call for the pieces, one for the query.
    let embedded = ctx.run.summary().usage.embedding_tokens;
    assert_eq!(embedded, split(&text, PIECE_CHARACTERS).len() as u64 + 1);
}

/// A corpus holding one page with a piece per vector, named p0, p1, ... in order.
async fn corpus_of(vectors: &[[f32; 2]]) -> Corpus {
    let mut corpus = Corpus::default();
    let texts = (0..vectors.len()).map(|i| format!("p{i}")).collect();
    let vectors = vectors.iter().map(|v| v.to_vec()).collect();
    corpus.add(Arc::new(at("https://a.org/x", "x")), texts, vectors).await;
    corpus
}

fn questioning() -> Tools {
    encoding(&[("q", &[1.0, 0.0])], None).1
}

async fn recalled(corpus: &mut Corpus) -> Vec<String> {
    let tools = questioning();
    let found = corpus.recall(&tools.encoder, "q", &ctx()).await.unwrap();
    found.into_iter().map(|(_, text)| text).collect()
}

/// What `recall` read, and the events it published in `ctx`.
#[allow(clippy::too_many_arguments)]
async fn recall_in(
    tools: &Tools,
    ctx: &RunCtx,
    corpus: &mut Corpus,
    first_id: u64,
    step: u64,
    label: &str,
    question: Option<u64>,
    parent: Option<u64>,
    stage: &str,
) -> Vec<Claim> {
    read::recall(tools, ctx, corpus, "b", "q", first_id, step, label, question, parent, stage, None)
        .await
        .unwrap()
}

#[tokio::test]
async fn recall_reads_the_pieces_held_into_claims_and_counts_them_for_their_site() {
    let mut corpus = Corpus::default();
    let found = Page {
        final_url: "https://www.a.org/x/".into(),
        title: "A".into(),
        ..at("https://a.org/x", "x")
    };
    let texts = vec!["It was found in 1904.".into(), "Far from it.".into()];
    corpus.add(Arc::new(found), texts, vec![vec![1.0, 0.0], vec![0.0, 1.0]]).await;
    let found = findings(&[FOUND]);
    let llm = Arc::new(
        ScriptedLlm::by_role(&[("intake", &[&found])]).with_vectors(&[("q", &[1.0, 0.0])], None),
    );
    let tools = tools(&llm, &["intake"]);
    let ctx = ctx();

    let stage = kvasir_run::stages::RESEARCH;
    let claims =
        recall_in(&tools, &ctx, &mut corpus, 5, 2, "recall for q", None, None, stage).await;

    let read: Vec<_> =
        claims.iter().map(|c| (c.id, c.url.as_str(), c.title.as_str(), c.step)).collect();
    assert_eq!(read, [(5, "https://www.a.org/x/", "A", 2)]);
    assert_eq!(corpus.claims, HashMap::from([("a.org".to_owned(), 1)]));
    assert_eq!(corpus.pieces.iter().map(|p| p.read).collect::<Vec<_>>(), [true, false]);
    let published: Vec<_> = events(&ctx)
        .into_iter()
        .map(|e| (e.stage, e.detail, e.done, e.total, e.kind, e.parent, e.question, e.page))
        .collect();
    let some = |kind: &str| Some(kind.to_owned());
    assert_eq!(
        published,
        [
            (
                stage.into(),
                "recall for q: 1 pieces from 1 pages".into(),
                Some(1),
                Some(1),
                some("recall"),
                None,
                None,
                None
            ),
            (
                stage.into(),
                "recall for q: read page 1/1: A".into(),
                Some(1),
                Some(1),
                some("part"),
                Some(0),
                None,
                Some(1)
            ),
            (
                stage.into(),
                "recall for q: 1 new claims, 5 in all".into(),
                Some(1),
                Some(5),
                some("result"),
                Some(0),
                None,
                None
            ),
        ]
    );
}

#[tokio::test]
async fn recall_numbers_its_pages_and_names_its_question_and_parent() {
    let mut corpus = Corpus::default();
    for url in ["https://a.org/x", "https://b.org/y"] {
        let texts = vec!["p0".into(), "p1".into()];
        corpus.add(Arc::new(at(url, "x")), texts, vec![vec![1.0, 0.0]; 2]).await;
    }
    let none = reply(&[("findings", "[]")]);
    let llm = Arc::new(
        ScriptedLlm::by_role(&[("intake", &[&none, &none])])
            .with_vectors(&[("q", &[1.0, 0.0])], None),
    );
    let tools = tools(&llm, &["intake"]);
    let ctx = ctx();

    let stage = kvasir_run::stages::VERIFY;
    recall_in(&tools, &ctx, &mut corpus, 1, 0, "recall", Some(7), Some(3), stage).await;

    let published = events(&ctx);
    let fields: Vec<_> = published
        .iter()
        .map(|e| (e.kind.clone().unwrap(), e.parent, e.page, e.done, e.total))
        .collect();
    assert_eq!(
        fields,
        [
            ("recall".into(), Some(3), None, Some(3), Some(2)),
            ("part".into(), Some(0), Some(1), Some(1), Some(2)),
            ("part".into(), Some(0), Some(2), Some(2), Some(2)),
            ("result".into(), Some(0), None, Some(0), Some(0)),
        ]
    );
    assert_eq!(published[0].question, Some(7));
    assert!(published.iter().all(|e| e.stage == stage));
}

#[tokio::test]
async fn recall_with_nothing_held_makes_no_call() {
    let llm = Arc::new(ScriptedLlm::replying(&[]).with_vectors(&[("q", &[1.0, 0.0])], None));
    let tools = tools(&llm, &["intake"]);
    let ctx = ctx();

    let stage = kvasir_run::stages::RESEARCH;
    let claims =
        recall_in(&tools, &ctx, &mut Corpus::default(), 1, 0, "recall for q", None, None, stage)
            .await;

    assert!(claims.is_empty());
    assert!(events(&ctx).is_empty());
}

#[tokio::test]
async fn recall_takes_the_pieces_at_or_above_the_threshold_best_first() {
    let below = (RECALL_MIN - 0.1) as f32;
    let off = [below, (1.0 - below * below).sqrt()];
    let mut corpus = corpus_of(&[[0.7, 0.714], [1.0, 0.0], off]).await;

    assert_eq!(recalled(&mut corpus).await, ["p1", "p0"]);
}

#[tokio::test]
async fn recall_stops_at_the_piece_limit() {
    let mut corpus = Corpus::default();
    for i in 0..=RECALL_PIECES {
        let url = format!("https://s{i}.org");
        corpus.add(Arc::new(at(&url, "x")), vec![format!("p{i}")], vec![vec![1.0, 0.0]]).await;
    }

    assert_eq!(recalled(&mut corpus).await.len(), RECALL_PIECES);
}

#[tokio::test]
async fn recall_takes_a_site_only_so_many_times() {
    let mut corpus = Corpus::default();
    let texts = vec!["a1".into(), "a2".into()];
    corpus.add(Arc::new(at("https://www.a.org/1", "x")), texts, vec![vec![1.0, 0.0]; 2]).await;
    corpus.add(Arc::new(at("https://a.org/2", "x")), vec!["a3".into()], vec![vec![1.0, 0.0]]).await;
    corpus.add(Arc::new(at("https://b.org", "x")), vec!["b1".into()], vec![vec![0.9, 0.1]]).await;

    assert_eq!(RECALL_PER_SITE, 2);
    assert_eq!(recalled(&mut corpus).await, ["a1", "a2", "b1"]);
}

#[tokio::test]
async fn a_piece_recalled_or_read_is_not_recalled_again() {
    let mut corpus = corpus_of(&[[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]).await;
    corpus.pieces[0].read = true;

    assert_eq!(recalled(&mut corpus).await, ["p1", "p2"]);
    assert!(recalled(&mut corpus).await.is_empty());
}

#[tokio::test]
async fn the_corpus_counts_pages_and_claims_per_site() {
    let mut corpus = Corpus::default();
    corpus.add(Arc::new(at("https://www.a.org/1", "x")), vec!["x".into()], vec![vec![1.0]]).await;
    corpus.add(Arc::new(at("https://a.org/2", "x")), vec!["y".into()], vec![vec![1.0]]).await;

    corpus.record(&[claim(1, "https://a.org/1"), claim(2, "https://b.org")]);

    assert_eq!(corpus.pages, HashMap::from([("a.org".to_owned(), 2)]));
    assert_eq!(corpus.claims, HashMap::from([("a.org".to_owned(), 1), ("b.org".to_owned(), 1)]));
}
