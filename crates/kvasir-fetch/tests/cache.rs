//! The page cache across runs: fresh, stale and revalidated reads, and what a failure leaves.

use std::collections::BTreeSet;
use std::path::Path;
use std::time::Duration;

use kvasir_db::rusqlite::Connection;
use kvasir_extract::{Link, Region};
use kvasir_fetch::{Engine, FetchError, Options, Page, PageStore, Web, system_lookup};
use tempfile::TempDir;
use wiremock::matchers::any;
use wiremock::{Mock, MockGuard, MockServer, ResponseTemplate};

const HOUR: f64 = 3600.0;

fn article(word: &str, href: &str) -> String {
    format!(
        "<html><body><article><p>{word} is what this page says, at some length, so that it reads \
         as an article.</p><p><a href=\"{href}\">more</a></p></article></body></html>"
    )
}

fn reply(body: String, etag: &str) -> ResponseTemplate {
    ResponseTemplate::new(200)
        .set_body_raw(body, "text/html")
        .insert_header("etag", etag)
        .insert_header("last-modified", "Mon, 01 Jan 2024 00:00:00 GMT")
}

struct Setting {
    server: MockServer,
    web: Web,
    store: PageStore,
    url: String,
    dir: TempDir,
}

impl Setting {
    async fn new() -> Self {
        let server = MockServer::start().await;
        let dir = TempDir::new().unwrap();
        let store = PageStore::open(&dir.path().join("pages.db")).unwrap();
        let options = Options {
            contact: String::new(),
            host_interval: Duration::ZERO,
            render: false,
            stealth: false,
            max_age_hours: 48,
        };
        let web = Web::new(options, 8, false, system_lookup());
        web.set_cache(Some(store.clone()));
        let url = format!("{}/a", server.uri());
        Self { server, web, store, url, dir }
    }

    async fn answer(&self, response: ResponseTemplate) -> MockGuard {
        Mock::given(any()).respond_with(response).mount_as_scoped(&self.server).await
    }

    async fn requests(&self) -> usize {
        self.server.received_requests().await.unwrap().len()
    }

    fn age(&self, hours: f64) {
        Connection::open(self.dir.path().join("pages.db"))
            .unwrap()
            .execute("UPDATE pages SET fetched_at = fetched_at - ?", [hours * HOUR])
            .unwrap();
    }
}

fn page(url: &str, text: &str) -> Page {
    Page {
        url: url.to_owned(),
        final_url: url.to_owned(),
        title: String::new(),
        text: text.to_owned(),
        engine: Engine::Static,
        links: Vec::new(),
        fetched_at: 1.0,
    }
}

fn store_in(dir: &Path) -> PageStore {
    PageStore::open(&dir.join("pages.db")).unwrap()
}

async fn search(store: &PageStore, words: &str) -> Vec<String> {
    let found = store.search(words, "m", &[], 200, &BTreeSet::new()).await.unwrap();
    found.into_iter().map(|found| found.text).collect()
}

#[tokio::test]
async fn a_second_fetch_makes_no_request() {
    let setting = Setting::new().await;
    let _site = setting.answer(reply(article("first", "/next"), "\"v1\"")).await;

    let first = setting.web.fetch_page(&setting.url, None).await.unwrap();
    let second = setting.web.fetch_page(&setting.url, None).await.unwrap();

    assert_eq!(setting.requests().await, 1);
    assert_eq!(second, first);
}

#[tokio::test]
async fn a_stale_copy_is_asked_for_again_and_a_304_refreshes_it() {
    let setting = Setting::new().await;
    let first = {
        let _site = setting.answer(reply(article("first", "/next"), "\"v1\"")).await;
        setting.web.fetch_page(&setting.url, None).await.unwrap()
    };
    setting.age(49.0);
    let _site = setting.answer(ResponseTemplate::new(304)).await;

    let second = setting.web.fetch_page(&setting.url, None).await.unwrap();

    let seen = setting.server.received_requests().await.unwrap();
    let sent = &seen[1].headers;
    assert_eq!(sent.get("if-none-match").unwrap(), "\"v1\"");
    assert_eq!(sent.get("if-modified-since").unwrap(), "Mon, 01 Jan 2024 00:00:00 GMT");
    assert_eq!(second.text, first.text);
    assert!(second.fetched_at > first.fetched_at);
    assert_eq!(
        setting.web.fetch_page(&setting.url, None).await.unwrap().fetched_at,
        second.fetched_at
    );
    assert_eq!(setting.requests().await, 2);
}

#[tokio::test]
async fn a_200_on_revalidation_replaces_the_text_and_the_links() {
    let setting = Setting::new().await;
    {
        let _site = setting.answer(reply(article("first", "/next"), "\"v1\"")).await;
        setting.web.fetch_page(&setting.url, None).await.unwrap();
    }
    setting.age(49.0);
    let second = ResponseTemplate::new(200)
        .set_body_raw(article("second", "/other"), "text/html")
        .insert_header("etag", "\"v2\"");
    let _site = setting.answer(second).await;

    let page = setting.web.fetch_page(&setting.url, None).await.unwrap();

    let other = format!("{}/other", setting.server.uri());
    assert!(page.text.contains("second"));
    assert_eq!(page.links.iter().map(|l| &l.url).collect::<Vec<_>>(), [&other]);
    let cached = setting.store.get(&setting.url).await.unwrap().unwrap();
    assert!(cached.page.text.contains("second"));
    assert_eq!((cached.etag.as_deref(), cached.last_modified), (Some("\"v2\""), None));
    assert_eq!(cached.page.links.iter().map(|l| &l.url).collect::<Vec<_>>(), [&other]);
}

#[tokio::test]
async fn a_max_age_of_zero_fetches_again_without_conditions_and_replaces_the_copy() {
    let setting = Setting::new().await;
    {
        let _site = setting.answer(reply(article("first", "/next"), "\"v1\"")).await;
        setting.web.fetch_page(&setting.url, None).await.unwrap();
    }
    setting
        .store
        .put_pieces(&setting.url, vec![("first".to_owned(), vec![1.0])], "m")
        .await
        .unwrap();
    let _site = setting.answer(reply(article("second", "/next"), "\"v2\"")).await;

    let page = setting.web.fetch_page(&setting.url, Some(0)).await.unwrap();

    let seen = setting.server.received_requests().await.unwrap();
    assert!(!seen[1].headers.contains_key("if-none-match"));
    assert!(page.text.contains("second"));
    assert!(setting.store.get(&setting.url).await.unwrap().unwrap().page.text.contains("second"));
    assert!(search(&setting.store, "first").await.is_empty(), "the pieces were of the old text");
}

#[tokio::test]
async fn a_max_age_for_one_read_does_not_reach_another() {
    let setting = Setting::new().await;
    let _site = setting.answer(reply(article("first", "/next"), "\"v1\"")).await;
    setting.web.fetch_page(&setting.url, Some(0)).await.unwrap();
    setting.web.fetch_page(&setting.url, None).await.unwrap();
    assert_eq!(setting.requests().await, 1);
}

#[tokio::test]
async fn a_failure_stores_nothing_and_keeps_the_old_copy() {
    let setting = Setting::new().await;
    {
        let _site = setting.answer(reply(article("first", "/next"), "\"v1\"")).await;
        setting.web.fetch_page(&setting.url, None).await.unwrap();
    }
    setting.age(49.0);
    let _site = setting.answer(ResponseTemplate::new(500)).await;
    let missing = format!("{}/missing", setting.server.uri());

    assert!(matches!(setting.web.fetch_page(&setting.url, None).await, Err(FetchError::Failed(_))));
    assert!(setting.web.fetch_page(&missing, None).await.is_err());

    assert!(setting.store.get(&setting.url).await.unwrap().unwrap().page.text.contains("first"));
    assert_eq!(setting.store.get(&missing).await.unwrap(), None);
}

#[tokio::test]
async fn spellings_of_one_url_share_a_row() {
    let dir = TempDir::new().unwrap();
    let store = store_in(dir.path());
    store.put(&page("https://www.example.org/a/", "x"), None, None).await.unwrap();

    let cached = store.get("http://example.org/a").await.unwrap().unwrap();

    assert_eq!(cached.page.url, "http://example.org/a");
    assert_eq!(store.rows().await.unwrap().len(), 1);
}

#[tokio::test]
async fn links_round_trip_with_their_regions() {
    let dir = TempDir::new().unwrap();
    let store = store_in(dir.path());
    let link = |url: &str, anchor: &str, context: &str, region| Link {
        url: url.to_owned(),
        anchor: anchor.to_owned(),
        context: context.to_owned(),
        region,
    };
    let page = Page {
        url: "https://example.org/a".to_owned(),
        final_url: "https://example.org/final".to_owned(),
        title: "T".to_owned(),
        text: "body".to_owned(),
        engine: Engine::Render,
        links: vec![
            link("https://b.example/x", "x", "see x here", Region::Main),
            link("https://b.example/y", "y", "", Region::Footer),
            link("https://b.example/z", "z", "also z", Region::Related),
        ],
        fetched_at: 12.5,
    };

    store.put(&page, None, None).await.unwrap();

    let cached = store.get(&page.url).await.unwrap().unwrap();
    assert_eq!((cached.page, cached.etag, cached.last_modified), (page, None, None));
    assert_eq!(store.link_pairs().await.unwrap().len(), 3);
}

#[tokio::test]
async fn flushing_frees_the_disk_and_the_text_index() {
    let dir = TempDir::new().unwrap();
    let store = store_in(dir.path());
    let big = "word ".repeat(20_000);
    let url = "https://example.org/a";
    store.put(&page(url, &big), None, None).await.unwrap();
    store.put_pieces(url, vec![(big, vec![1.0, 0.0])], "m").await.unwrap();
    let before = store.stats().await.unwrap().file_bytes;
    assert!(!search(&store, "word").await.is_empty());

    store.flush().await.unwrap();

    let after = store.stats().await.unwrap();
    assert_eq!((after.pages, after.pieces, after.text_bytes, after.vector_bytes), (0, 0, 0, 0));
    assert!(after.file_bytes < before, "{} < {before}", after.file_bytes);
    assert!(search(&store, "word").await.is_empty());
    let db = Connection::open(dir.path().join("pages.db")).unwrap();
    let indexed: i64 = db.query_row("SELECT COUNT(*) FROM pieces_fts", [], |r| r.get(0)).unwrap();
    assert_eq!(indexed, 0);
}

#[tokio::test]
async fn concurrent_puts_do_not_error() {
    let dir = TempDir::new().unwrap();
    let store = store_in(dir.path());
    let mut tasks = Vec::new();
    for n in 0..6 {
        let store = store.clone();
        tasks.push(tokio::spawn(async move {
            for i in 0..10 {
                let mut stored =
                    page(&format!("https://example.org/{}", n % 3), &format!("text {i}"));
                stored.fetched_at = f64::from(i);
                store.put(&stored, None, None).await.unwrap();
            }
        }));
    }
    for task in tasks {
        task.await.unwrap();
    }
    let cached = store.get("https://example.org/0").await.unwrap().unwrap();
    assert_eq!(cached.page.text, "text 9");
}

#[tokio::test]
async fn a_failing_cache_costs_the_download_and_not_the_run() {
    let setting = Setting::new().await;
    let _site = setting.answer(reply(article("first", "/next"), "\"v1\"")).await;
    Connection::open(setting.dir.path().join("pages.db"))
        .unwrap()
        .execute_batch(
            "DROP TABLE links; DROP TRIGGER pieces_dropped; DROP TABLE pieces; DROP TABLE pages;",
        )
        .unwrap();

    let page = setting.web.fetch_page(&setting.url, None).await.unwrap();

    assert!(page.text.contains("first"));
    assert_eq!(setting.requests().await, 1);
}

#[tokio::test]
async fn search_finds_by_word_and_model_and_leaves_out_what_it_is_told_to() {
    let dir = TempDir::new().unwrap();
    let store = store_in(dir.path());
    for url in ["https://a.org/x", "https://www.b.org/y"] {
        store.put(&page(url, "x"), None, None).await.unwrap();
        let piece = format!("the stone at {url}");
        store.put_pieces(url, vec![(piece, vec![1.0, 0.5])], "m").await.unwrap();
    }

    let found = store.search("stone", "m", &[], 200, &BTreeSet::new()).await.unwrap();
    assert_eq!(found.len(), 2);
    assert_eq!(found[0].vector, [1.0, 0.5]);
    assert_eq!(found[0].page.text, "");

    assert!(store.search("stone", "old", &[], 200, &BTreeSet::new()).await.unwrap().is_empty());
    let one = store.search("stone", "m", &[], 1, &BTreeSet::new()).await.unwrap();
    assert_eq!(one.len(), 1);
    let urls = ["https://a.org/x/".to_owned()];
    let rest = store.search("stone", "m", &urls, 200, &BTreeSet::new()).await.unwrap();
    assert_eq!(rest[0].page.url, "https://b.org/y");
    assert_eq!(rest.len(), 1);
    let hosts = BTreeSet::from(["b.org".to_owned()]);
    let rest = store.search("stone", "m", &[], 200, &hosts).await.unwrap();
    assert_eq!(rest[0].page.url, "https://a.org/x");
    assert_eq!(rest.len(), 1);
}

#[tokio::test]
async fn a_question_without_words_finds_nothing() {
    let dir = TempDir::new().unwrap();
    let store = store_in(dir.path());
    assert!(search(&store, "?! -- ()").await.is_empty());
    assert!(search(&store, "\"").await.is_empty());
}

#[tokio::test]
async fn delete_drops_a_page_with_its_links_and_pieces() {
    let dir = TempDir::new().unwrap();
    let store = store_in(dir.path());
    let url = "https://a.org/x";
    let mut stored = page(url, "x");
    stored.links = vec![Link {
        url: "https://b.org/".to_owned(),
        anchor: String::new(),
        context: String::new(),
        region: Region::Main,
    }];
    store.put(&stored, None, None).await.unwrap();
    store.put_pieces(url, vec![("stone".to_owned(), vec![1.0])], "m").await.unwrap();

    let keys = store.rows().await.unwrap().into_iter().map(|row| row.key).collect();
    store.delete(keys).await.unwrap();

    let stats = store.stats().await.unwrap();
    assert_eq!((stats.pages, stats.links, stats.pieces), (0, 0, 0));
    assert!(search(&store, "stone").await.is_empty());
}

#[tokio::test]
async fn a_page_stored_under_another_spelling_moves_at_open_and_the_newest_wins() {
    let dir = TempDir::new().unwrap();
    drop(store_in(dir.path()));
    let path = dir.path().join("pages.db");
    let db = Connection::open(&path).unwrap();
    let add = |url: &str, at: f64, text: &str| {
        db.execute(
            "INSERT INTO pages VALUES (?, ?, ?, NULL, NULL, 'h', 'static', '', ?)",
            kvasir_db::params![url, url, at, text],
        )
        .unwrap();
    };
    add("https://www.example.org/a", 5.0, "old spelling, newer");
    add("https://example.org/a", 1.0, "new spelling, older");
    add("https://www.example.org/b", 1.0, "moves");
    db.execute(
        "INSERT INTO pieces VALUES ('https://www.example.org/b', 0, 'stone', 'm', x'0000803f')",
        [],
    )
    .unwrap();
    drop(db);

    let store = store_in(dir.path());

    assert_eq!(store.rows().await.unwrap().len(), 2);
    let kept = store.get("https://example.org/a").await.unwrap().unwrap();
    assert_eq!(kept.page.text, "old spelling, newer");
    let moved = store.search("stone", "m", &[], 10, &BTreeSet::new()).await.unwrap();
    assert_eq!(moved[0].page.url, "https://example.org/b");
    assert_eq!(moved[0].vector, [1.0]);
}
