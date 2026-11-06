//! Leads: the links of pages read, followed when they look worth more than a search result.

mod common;

use std::collections::HashSet;
use std::sync::Arc;

use common::{Fetched, Tiers, claim, ctx, tools};
use kvasir_extract::{Link, Region};
use kvasir_fetch::{Engine, Page};
use kvasir_research::claims::Claim;
use kvasir_research::corpus::{Corpus, LEAD_DEPTH, PAGES_PER_SITE, SITE_AFFINITY, SITE_SEARCHES};
use kvasir_research::read::read;
use kvasir_research::search::{Ranking, search};
use kvasir_research::{Classify, Hit, Tools};
use kvasir_testkit::{ScriptedLlm, reply};
use kvasir_types::sites::Tier;

const QUESTION: &str = "when was the stone found";

/// An intake model that reads one claim from each text, and an encoder in which a text about the
/// stone points one way and any other text another.
fn keyword() -> Arc<ScriptedLlm> {
    let llm = ScriptedLlm::new(|request| {
        let text = &request.messages[1].content;
        let quote = if text.contains("the snippet") { "the snippet" } else { "text of the page" };
        let found = format!(r#"[{{"claim": "c", "quote": "{quote}", "confidence": "high"}}]"#);
        Ok(reply(&[("findings", &found)]))
    });
    let embedding =
        |text: &str| if text.contains("stone") { vec![1.0, 0.0] } else { vec![0.0, 1.0] };
    Arc::new(llm.with_embedding(embedding))
}

fn link(url: &str, anchor: &str, region: Region) -> Link {
    Link { url: url.into(), anchor: anchor.into(), context: String::new(), region }
}

fn main(url: &str, anchor: &str) -> Link {
    link(url, anchor, Region::Main)
}

fn page(url: &str, links: Vec<Link>) -> Page {
    Page {
        url: url.into(),
        final_url: url.into(),
        title: String::new(),
        text: "The text of the page.".into(),
        engine: Engine::Static,
        links,
        fetched_at: 0.0,
    }
}

fn result(url: &str) -> Hit {
    Hit { url: url.into(), title: "start".into(), ..Hit::default() }
}

/// Tools serving `web`, its pages kept for the test to read what was fetched.
fn serving(web: Fetched, sources: Option<Arc<dyn Classify>>) -> (Tools, Arc<Fetched>) {
    let web = Arc::new(web);
    let mut tools = tools(&keyword(), &["intake"]);
    tools.pages = web.clone();
    tools.sources = sources;
    (tools, web)
}

/// One search that finds `hits`, and the reading of what it picks.
async fn follow(
    tools: &mut Tools,
    corpus: &mut Corpus,
    hits: Vec<Hit>,
    claims: &[Claim],
    seen: &mut HashSet<String>,
) -> Vec<Hit> {
    tools.search = Arc::new(common::Results::new(hits, Vec::new()));
    let ctx = ctx();
    let ranking = Ranking {
        encoder: Some(&tools.encoder),
        question: QUESTION,
        held: None,
        corpus: Some(corpus),
        claims,
    };
    let picked = search(tools, &ctx, &["q".into()], 1, seen, ranking).await.unwrap();
    read_all(tools, corpus, &picked).await;
    picked
}

async fn read_all(tools: &Tools, corpus: &mut Corpus, hits: &[Hit]) -> Vec<Claim> {
    let stage = kvasir_run::stages::RESEARCH;
    read(tools, &ctx(), corpus, "brief", QUESTION, hits, 1, 1, "s", None, stage).await.unwrap()
}

fn urls(hits: &[Hit]) -> Vec<&str> {
    hits.iter().map(|hit| hit.url.as_str()).collect()
}

fn keys(corpus: &Corpus) -> Vec<&str> {
    corpus.leads.keys().map(String::as_str).collect()
}

async fn add(corpus: &mut Corpus, tools: &Tools, page: &Page, depth: u32) {
    corpus.add_leads(&tools.encoder, page, depth, &ctx()).await.unwrap();
}

async fn leads_for(
    corpus: &Corpus,
    tools: &Tools,
    held: Option<&[Vec<f32>]>,
    claims: &[Claim],
    limit: usize,
) -> Vec<Hit> {
    let ctx = ctx();
    corpus
        .leads_for(&tools.encoder, QUESTION, held, claims, &HashSet::new(), limit, &ctx)
        .await
        .unwrap()
}

#[tokio::test]
async fn a_run_follows_a_related_link_that_matches_a_question_and_skips_the_menu() {
    let start = page(
        "https://a.org/start",
        vec![
            link("https://b.org/the-stone-found", "The stone found", Region::Related),
            link("https://c.org/stone-menu", "Stone", Region::Nav),
            main("https://d.org/taxes", "Tax rules"),
        ],
    );
    let web = Fetched::of([start.clone(), page("https://b.org/the-stone-found", vec![])]);
    let (mut tools, web) = serving(web, None);
    let (mut corpus, mut seen) = (Corpus::default(), HashSet::new());

    follow(&mut tools, &mut corpus, vec![result(&start.url)], &[], &mut seen).await;
    let picked = follow(&mut tools, &mut corpus, vec![result(&start.url)], &[], &mut seen).await;

    assert_eq!(urls(&picked), ["https://b.org/the-stone-found"]);
    assert_eq!(picked[0].depth, Some(1));
    assert_eq!(web.fetched(), [start.url.as_str(), "https://b.org/the-stone-found"]);
    let held: HashSet<&str> = keys(&corpus).into_iter().collect();
    assert_eq!(held, HashSet::from(["https://b.org/the-stone-found", "https://d.org/taxes"]));
}

#[tokio::test]
async fn a_run_reads_no_more_than_the_pages_per_site() {
    let start = page("https://a.org/start", vec![main("https://b.org/stone", "The stone found")]);
    let web = Fetched::of([start.clone(), page("https://b.org/stone", vec![])]);
    let (mut tools, web) = serving(web, None);
    let mut corpus = Corpus::default();
    follow(&mut tools, &mut corpus, vec![result(&start.url)], &[], &mut HashSet::new()).await;
    corpus.pages.insert("b.org".into(), PAGES_PER_SITE);

    let other = vec![result("https://x.org/other")];
    let picked = follow(&mut tools, &mut corpus, other, &[], &mut HashSet::new()).await;

    assert_eq!(urls(&picked), ["https://x.org/other"]);
    assert!(!web.fetched().contains(&"https://b.org/stone".to_owned()));
    // A search result of that site is skipped as well.
    let more = vec![result("https://b.org/more")];
    assert!(follow(&mut tools, &mut corpus, more, &[], &mut HashSet::new()).await.is_empty());
}

#[tokio::test]
async fn a_lead_below_the_minimum_is_not_read_however_thin_the_search() {
    let start = page("https://a.org/start", vec![main("https://d.org/taxes", "Tax rules")]);
    let (mut tools, web) = serving(Fetched::of([start.clone()]), None);
    let mut corpus = Corpus::default();
    follow(&mut tools, &mut corpus, vec![result(&start.url)], &[], &mut HashSet::new()).await;

    assert!(follow(&mut tools, &mut corpus, vec![], &[], &mut HashSet::new()).await.is_empty());
    assert_eq!(web.fetched(), [start.url]);
}

#[tokio::test]
async fn a_lead_whose_page_cannot_be_read_gives_no_claim_but_a_result_falls_back() {
    let llm = keyword();
    let mut tools = tools(&llm, &["intake"]);
    tools.pages = Arc::new(Fetched::default());
    let lead = Hit {
        url: "https://b.org/gone".into(),
        title: "anchor".into(),
        description: "what a page says".into(),
        depth: Some(1),
        ..Hit::default()
    };
    let snippet = Hit {
        url: "https://x.org/gone".into(),
        title: "t".into(),
        description: "the snippet".into(),
        ..Hit::default()
    };

    assert!(read_all(&tools, &mut Corpus::default(), &[lead]).await.is_empty());
    assert!(llm.sent().is_empty());

    let claims = read_all(&tools, &mut Corpus::default(), &[snippet]).await;
    assert_eq!(llm.sent().len(), 1);
    assert!(llm.sent()[0].messages[1].content.contains("the snippet"));
    assert!(claims[0].from_snippet);
}

#[tokio::test]
async fn a_lead_takes_the_title_of_the_page_it_led_to() {
    let found = Page { title: "Stone find, 1904".into(), ..page("https://b.org/stone", vec![]) };
    let (tools, _) = serving(Fetched::of([found]), None);
    let lead = Hit {
        url: "https://b.org/stone".into(),
        title: "anchor".into(),
        depth: Some(1),
        ..Hit::default()
    };

    let claims = read_all(&tools, &mut Corpus::default(), &[lead]).await;

    assert_eq!(claims[0].title, "Stone find, 1904");
}

#[tokio::test]
async fn robots_txt_refusing_a_lead_keeps_it_unfetched_but_not_a_result_of_its_host() {
    let start =
        page("https://a.org/start", vec![main("https://b.org/private/stone", "The stone found")]);
    let web = Fetched {
        refused: vec!["/private/".into()],
        ..Fetched::of([start.clone(), page("https://b.org/result", vec![])])
    };
    let (mut tools, web) = serving(web, None);
    let mut corpus = Corpus::default();
    follow(&mut tools, &mut corpus, vec![result(&start.url)], &[], &mut HashSet::new()).await;

    assert!(follow(&mut tools, &mut corpus, vec![], &[], &mut HashSet::new()).await.is_empty());
    assert!(!corpus.leads.contains_key("https://b.org/private/stone"));
    let other = vec![result("https://b.org/result")];
    assert!(!follow(&mut tools, &mut corpus, other, &[], &mut HashSet::new()).await.is_empty());
    assert_eq!(web.fetched(), [start.url.as_str(), "https://b.org/result"]);
    // Dropped for good: another page linking to it does not bring it back.
    let again = page("https://e.org/x", vec![main("https://b.org/private/stone", "stone")]);
    add(&mut corpus, &tools, &again, 1).await;
    assert!(corpus.leads.is_empty());
}

#[tokio::test]
async fn a_lead_is_read_at_the_url_its_page_wrote_and_two_spellings_are_one_lead() {
    let linking = |url: &str, target: &str| Page {
        text: "text".into(),
        links: vec![Link { context: "c".into(), ..link(target, "stone", Region::Related) }],
        ..page(url, vec![])
    };
    let (tools, _) = serving(Fetched::default(), None);
    let mut corpus = Corpus::default();
    add(&mut corpus, &tools, &linking("https://a.org/x", "http://www.b.org/article/"), 1).await;
    add(&mut corpus, &tools, &linking("https://a.org/y", "https://b.org/article"), 1).await;

    let read: Vec<&str> = corpus.leads.values().map(|lead| lead.url.as_str()).collect();
    assert_eq!(read, ["http://www.b.org/article/"]);
    corpus.refuse("https://www.b.org/article");
    assert!(corpus.leads.is_empty());
}

#[tokio::test]
async fn a_lead_of_a_page_at_the_depth_limit_is_not_kept() {
    let start = page("https://a.org/x", vec![main("https://b.org/stone", "stone")]);
    let (tools, _) = serving(Fetched::default(), None);
    let mut corpus = Corpus::default();

    add(&mut corpus, &tools, &start, LEAD_DEPTH + 1).await;
    assert!(corpus.leads.is_empty());

    add(&mut corpus, &tools, &start, LEAD_DEPTH).await;
    assert_eq!(corpus.leads.values().map(|lead| lead.depth).collect::<Vec<_>>(), [LEAD_DEPTH]);
}

#[tokio::test]
async fn a_page_read_as_a_lead_at_depth_two_leaves_no_lead() {
    let found = page("https://b.org/stone", vec![main("https://c.org/stone", "stone")]);
    let (tools, _) = serving(Fetched::of([found]), None);
    let lead = Hit {
        url: "https://b.org/stone".into(),
        title: "anchor".into(),
        depth: Some(LEAD_DEPTH),
        ..Hit::default()
    };
    let mut corpus = Corpus::default();

    read_all(&tools, &mut corpus, &[lead]).await;

    assert!(corpus.leads.is_empty());
}

#[tokio::test]
async fn a_page_keeps_related_links_first_none_of_the_menu_and_a_url_once() {
    let links = vec![
        main("https://b.org/1", "one"),
        link("https://b.org/nav", "n", Region::Nav),
        link("https://b.org/foot", "f", Region::Footer),
        link("https://b.org/2", "two", Region::Related),
        main("https://a.org/x", "self"),
    ];
    let (tools, _) = serving(Fetched::default(), None);
    let mut corpus = Corpus::default();
    add(&mut corpus, &tools, &page("https://a.org/x", links), 1).await;
    let first = corpus.leads["https://b.org/1"].clone();

    let again = page("https://z.org/x", vec![main("https://b.org/1", "again")]);
    add(&mut corpus, &tools, &again, 2).await;

    assert_eq!(keys(&corpus), ["https://b.org/2", "https://b.org/1"]);
    let kept = &corpus.leads["https://b.org/1"];
    assert_eq!((kept.anchor.as_str(), kept.depth), (first.anchor.as_str(), first.depth));
}

#[tokio::test]
async fn a_related_link_outranks_an_otherwise_equal_link_in_the_text() {
    let (tools, _) = serving(Fetched::default(), None);
    let mut corpus = Corpus::default();
    add(&mut corpus, &tools, &page("https://a.org/x", vec![main("https://b.org/1", "stone")]), 1)
        .await;
    let related = vec![link("https://d.org/2", "stone", Region::Related)];
    add(&mut corpus, &tools, &page("https://c.org/x", related), 1).await;

    let found = leads_for(&corpus, &tools, None, &[], 2).await;

    assert_eq!(urls(&found), ["https://d.org/2", "https://b.org/1"]);
}

#[tokio::test]
async fn a_site_whose_claims_are_confirmed_outranks_one_that_is_not() {
    let (tools, _) = serving(Fetched::default(), None);
    let mut corpus = Corpus::default();
    add(&mut corpus, &tools, &page("https://a.org/x", vec![main("https://b.org/1", "stone")]), 1)
        .await;
    add(&mut corpus, &tools, &page("https://c.org/x", vec![main("https://d.org/2", "stone")]), 1)
        .await;
    corpus.pages.insert("d.org".into(), 1);
    let mut confirmed = claim(1, "https://d.org/old");
    confirmed.corroborated_by = vec![2];

    let found = leads_for(&corpus, &tools, None, &[confirmed], 2).await;

    assert_eq!(urls(&found), ["https://d.org/2", "https://b.org/1"]);
}

#[tokio::test]
async fn a_lead_a_claim_held_already_covers_ranks_below_a_new_one() {
    let (tools, _) = serving(Fetched::default(), None);
    let mut corpus = Corpus::default();
    add(&mut corpus, &tools, &page("https://a.org/x", vec![main("https://b.org/1", "stone")]), 1)
        .await;
    corpus.leads.get_mut("https://b.org/1").unwrap().vector = vec![1.0, 1.0];
    add(&mut corpus, &tools, &page("https://c.org/x", vec![main("https://d.org/2", "stone")]), 1)
        .await;
    corpus.leads.get_mut("https://d.org/2").unwrap().vector = vec![1.0, -1.0];

    let held = vec![vec![1.0, 1.0]];
    let found = leads_for(&corpus, &tools, Some(&held), &[], 2).await;

    assert_eq!(urls(&found), ["https://d.org/2", "https://b.org/1"]);
}

fn confirmed(site: &str, count: u64) -> Vec<Claim> {
    (0..count)
        .map(|i| Claim { corroborated_by: vec![99], ..claim(i, &format!("https://{site}/{i}")) })
        .collect()
}

#[tokio::test]
async fn a_site_search_fires_once_per_question_and_site_at_or_above_the_affinity() {
    let mut corpus = Corpus::default();
    corpus.pages.insert("a.org".into(), 2);
    let claims = confirmed("a.org", 2);
    const { assert!(SITE_AFFINITY <= 1.0) };

    assert_eq!(corpus.site_query(1, "when", &claims).await.as_deref(), Some("site:a.org when"));
    assert_eq!(corpus.site_query(1, "when", &claims).await, None);
    assert_eq!(corpus.site_query(2, "where", &claims).await.as_deref(), Some("site:a.org where"));
}

#[tokio::test]
async fn a_site_search_needs_the_affinity_and_a_site_with_pages_left() {
    let mut corpus = Corpus::default();
    corpus.pages.insert("a.org".into(), 4);
    assert_eq!(corpus.site_query(1, "when", &confirmed("a.org", 3)).await, None);

    corpus.pages.insert("b.org".into(), PAGES_PER_SITE);
    let full = confirmed("b.org", PAGES_PER_SITE as u64);
    assert_eq!(corpus.site_query(1, "when", &full).await, None);
}

#[tokio::test]
async fn a_run_makes_no_more_than_the_site_searches() {
    let mut corpus = Corpus::default();
    let mut claims = Vec::new();
    for site in ["a.org", "b.org", "c.org"] {
        corpus.pages.insert(site.into(), 2);
        claims.extend(confirmed(site, 2));
    }

    let mut found = Vec::new();
    for i in 0..=SITE_SEARCHES as u64 {
        found.push(corpus.site_query(i, "when", &claims).await);
    }

    assert!(found[..SITE_SEARCHES].iter().all(Option::is_some));
    assert_eq!(found[SITE_SEARCHES], None);
}

fn avoiding(tiers: &[(&str, Tier)]) -> Option<Arc<dyn Classify>> {
    Some(Arc::new(Tiers::new(tiers)))
}

#[tokio::test]
async fn a_result_of_an_avoid_site_is_never_fetched_even_when_it_is_the_only_one() {
    let sources = avoiding(&[("bad.org", Tier::Avoid)]);
    let (mut tools, web) =
        serving(Fetched::of([page("https://bad.org/x", vec![])]), sources.clone());
    let mut corpus = Corpus::new(None, "m".into(), false, sources);

    let picked = follow(
        &mut tools,
        &mut corpus,
        vec![result("https://bad.org/x")],
        &[],
        &mut HashSet::new(),
    )
    .await;

    assert!(picked.is_empty());
    assert!(web.fetched().is_empty());
}

/// Tiers that also name the lists behind them.
struct Listing(Tiers);

impl Classify for Listing {
    fn tiers<'a>(
        &'a self,
        hosts: Vec<String>,
        deadline: std::time::Duration,
    ) -> kvasir_llm::BoxFuture<'a, std::collections::HashMap<String, Tier>> {
        self.0.tiers(hosts, deadline)
    }

    fn listed_by<'a>(
        &'a self,
        _hosts: Vec<String>,
    ) -> kvasir_llm::BoxFuture<'a, std::collections::HashMap<String, Vec<String>>> {
        Box::pin(async { [("bad.org".to_owned(), vec!["Spam".to_owned()])].into() })
    }
}

#[tokio::test]
async fn a_run_is_told_which_sites_it_left_out_and_the_lists_behind_them() {
    let sources: Option<Arc<dyn Classify>> = Some(Arc::new(Listing(Tiers::new(&[
        ("bad.org", Tier::Avoid),
        ("worse.org", Tier::Avoid),
    ]))));
    let (mut tools, _) = serving(Fetched::of([]), sources);
    let hits = ["https://bad.org/x", "https://bad.org/y", "https://worse.org/x"];
    tools.search = Arc::new(common::Results::new(hits.map(result).to_vec(), Vec::new()));
    let ctx = ctx();
    ctx.publish(kvasir_types::runs::Progress::new(kvasir_run::stages::RESEARCH, "started"));

    let mut seen = HashSet::new();
    search(&tools, &ctx, &["q".into()], 1, &mut seen, Ranking::default()).await.unwrap();

    let events = ctx.run.detail().events;
    let last = events.last().unwrap();
    assert_eq!(last.detail, "left out avoided sites: bad.org (Spam), worse.org");
    assert_eq!(last.parent, Some(0));
}

#[tokio::test]
async fn a_lead_of_an_avoid_site_is_never_fetched() {
    let start = page(
        "https://a.org/start",
        vec![
            main("https://bad.org/stone", "The stone found"),
            main("https://b.org/stone", "stone"),
        ],
    );
    let pages =
        [start.clone(), page("https://bad.org/stone", vec![]), page("https://b.org/stone", vec![])];
    let sources = avoiding(&[("bad.org", Tier::Avoid)]);
    let (mut tools, web) = serving(Fetched::of(pages), sources.clone());
    let mut corpus = Corpus::new(None, "m".into(), false, sources);
    follow(&mut tools, &mut corpus, vec![result(&start.url)], &[], &mut HashSet::new()).await;

    let found = leads_for(&corpus, &tools, None, &[], 5).await;

    assert_eq!(urls(&found), ["https://b.org/stone"]);
    assert!(!web.fetched().contains(&"https://bad.org/stone".to_owned()));
}

#[tokio::test]
async fn a_lead_of_a_trusted_site_outranks_an_otherwise_equal_one() {
    let sources = avoiding(&[("b.org", Tier::High), ("d.org", Tier::Low)]);
    let (tools, _) = serving(Fetched::default(), sources.clone());
    let mut corpus = Corpus::new(None, "m".into(), false, sources);
    for site in ["b.org", "c.org", "d.org"] {
        let start = page("https://a.org/x", vec![main(&format!("https://{site}/1"), "stone")]);
        add(&mut corpus, &tools, &start, 1).await;
    }

    let found = leads_for(&corpus, &tools, None, &[], 3).await;

    assert_eq!(urls(&found), ["https://b.org/1", "https://c.org/1", "https://d.org/1"]);
}

/// A claim of `site` that a claim of `by_site` corroborates, and that claim.
fn corroborated(site: &str, by_site: &str) -> Vec<Claim> {
    let first = Claim { corroborated_by: vec![2], ..claim(1, &format!("https://{site}/1")) };
    vec![first, claim(2, &format!("https://{by_site}/1"))]
}

#[tokio::test]
async fn a_corroboration_counts_by_the_tier_of_the_site_that_made_it() {
    let tiers = [("good.org", Tier::High), ("low.org", Tier::Low), ("bad.org", Tier::Avoid)];
    let mut corpus = Corpus::new(None, "m".into(), false, avoiding(&tiers));
    corpus.pages.insert("a.org".into(), 1);

    let mut found = Vec::new();
    for by_site in ["good.org", "other.org", "low.org", "bad.org"] {
        let affinity = corpus.affinity(&corroborated("a.org", by_site)).await;
        found.push(affinity.get("a.org").copied().unwrap_or(0.0));
    }

    assert_eq!(found, [1.0, 0.5, 0.25, 0.0]);
}

#[tokio::test]
async fn a_site_search_is_refused_for_a_site_that_is_not_high_or_normal() {
    let mut claims = confirmed("a.org", 2);
    claims.push(claim(99, "https://b.org/1"));
    for (tier, searched) in
        [(Tier::High, true), (Tier::Normal, true), (Tier::Unknown, false), (Tier::Low, false)]
    {
        let sources = avoiding(&[("a.org", tier), ("b.org", Tier::High)]);
        let mut corpus = Corpus::new(None, "m".into(), false, sources);
        corpus.pages.insert("a.org".into(), 2);

        assert_eq!(corpus.site_query(1, "when", &claims).await.is_some(), searched, "{tier:?}");
    }
}

#[tokio::test]
async fn the_old_behaviour_holds_without_a_classifier() {
    let mut corpus = Corpus::default();
    corpus.pages.insert("a.org".into(), 1);

    assert!(corpus.sources.is_none());
    let claims = confirmed("a.org", 2);
    assert_eq!(corpus.site_query(1, "when", &claims).await.as_deref(), Some("site:a.org when"));
    let affinity = corpus.affinity(&corroborated("a.org", "b.org")).await;
    assert_eq!(affinity.into_iter().collect::<Vec<_>>(), [("a.org".to_owned(), 1.0)]);
}
