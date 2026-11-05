//! Reading pages from a local site, with the guard letting the site on 127.0.0.1 through.

use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use kvasir_browser::{BoxFuture, Render, RenderError, Rendered};
use kvasir_fetch::{Engine, FetchError, Guard, Options, Web, system_lookup};
use wiremock::matchers::{any, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn options() -> Options {
    Options {
        contact: String::new(),
        host_interval: Duration::ZERO,
        render: true,
        stealth: false,
        max_age_hours: 48,
    }
}

fn web() -> Web {
    Web::new(options(), 8, Guard::Loopback, system_lookup())
}

fn fixture(name: &str) -> String {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/pages");
    std::fs::read_to_string(root.join(name)).unwrap()
}

fn html(body: String) -> ResponseTemplate {
    ResponseTemplate::new(200).set_body_raw(body, "text/html")
}

async fn site(reply: ResponseTemplate) -> MockServer {
    let server = MockServer::start().await;
    Mock::given(any()).respond_with(reply).mount(&server).await;
    server
}

/// A browser that answers from a list, and says what it was asked.
struct FakeBrowser {
    answer: Result<(String, u16), RenderError>,
    asked: Mutex<Vec<String>>,
}

impl FakeBrowser {
    fn answering(html: String, status: u16) -> Arc<Self> {
        Arc::new(Self { answer: Ok((html, status)), asked: Mutex::new(Vec::new()) })
    }

    fn asked(&self) -> Vec<String> {
        self.asked.lock().unwrap().clone()
    }
}

impl Render for FakeBrowser {
    fn render<'a>(&'a self, url: &'a str) -> BoxFuture<'a, Result<Rendered, RenderError>> {
        self.asked.lock().unwrap().push(url.to_owned());
        Box::pin(async move {
            match &self.answer {
                Ok((html, status)) => {
                    Ok(Rendered { html: html.clone(), final_url: url.to_owned(), status: *status })
                }
                Err(RenderError::Unavailable(why)) => Err(RenderError::Unavailable(why.clone())),
                Err(RenderError::Failed(why)) => Err(RenderError::Failed(why.clone())),
            }
        })
    }
}

fn article() -> String {
    fixture("cookie_wall.html")
}

#[tokio::test]
async fn a_shell_or_a_blocked_page_is_rendered() {
    for reply in [
        html(fixture("spa_shell.html")),
        ResponseTemplate::new(403).set_body_string("blocked"),
        ResponseTemplate::new(503).set_body_string("wait"),
    ] {
        let server = site(reply).await;
        let (web, browser) = (web(), FakeBrowser::answering(article(), 200));
        web.set_renderer(Some(browser.clone()));
        let url = format!("{}/app", server.uri());

        let page = web.fetch_page(&url, None).await.unwrap();

        assert_eq!(page.engine, Engine::Render);
        assert_eq!(browser.asked(), [url]);
    }
}

#[tokio::test]
async fn a_blocked_page_is_rendered_after_its_permit_is_released() {
    let server = site(ResponseTemplate::new(403)).await;
    // One permit in all: a render taking its own while the fetch held it would never start.
    let web = Web::new(options(), 1, Guard::Loopback, system_lookup());
    web.set_renderer(Some(FakeBrowser::answering(article(), 200)));

    let url = server.uri();
    let page = tokio::time::timeout(Duration::from_secs(10), web.fetch_page(&url, None));

    assert_eq!(page.await.expect("not waiting on itself").unwrap().engine, Engine::Render);
}

#[tokio::test]
async fn a_stealth_render_is_named_so() {
    let server = site(html(fixture("spa_shell.html"))).await;
    let web = web();
    web.configure(Options { stealth: true, ..options() });
    web.set_renderer(Some(FakeBrowser::answering(article(), 200)));

    assert_eq!(web.fetch_page(&server.uri(), None).await.unwrap().engine, Engine::Stealth);
}

#[tokio::test]
async fn without_a_browser_the_static_failure_stands() {
    let server = site(html(fixture("spa_shell.html"))).await;
    let web = web();
    assert!(matches!(web.fetch_page(&server.uri(), None).await, Err(FetchError::Unusable(_))));

    let unavailable = Arc::new(FakeBrowser {
        answer: Err(RenderError::Unavailable("no chromium".to_owned())),
        asked: Mutex::new(Vec::new()),
    });
    web.set_renderer(Some(unavailable));
    assert!(matches!(web.fetch_page(&server.uri(), None).await, Err(FetchError::Unusable(_))));
}

#[tokio::test]
async fn with_render_off_no_browser_is_asked() {
    let server = site(html(fixture("spa_shell.html"))).await;
    let (web, browser) = (web(), FakeBrowser::answering(article(), 200));
    web.configure(Options { render: false, ..options() });
    web.set_renderer(Some(browser.clone()));

    assert!(web.fetch_page(&server.uri(), None).await.is_err());
    assert!(browser.asked().is_empty());
}

#[tokio::test]
async fn a_render_that_is_still_a_challenge_or_an_error_fails() {
    for (body, status) in [(fixture("challenge_cloudflare.html"), 200), (article(), 404)] {
        let server = site(html(fixture("spa_shell.html"))).await;
        let web = web();
        web.set_renderer(Some(FakeBrowser::answering(body, status)));
        assert!(web.fetch_page(&server.uri(), None).await.is_err());
    }
}

#[tokio::test]
async fn a_429_is_waited_out_and_retried_once() {
    let server = MockServer::start().await;
    Mock::given(any())
        .respond_with(ResponseTemplate::new(429).insert_header("retry-after", "1"))
        .up_to_n_times(1)
        .mount(&server)
        .await;
    Mock::given(any()).respond_with(html(article())).mount(&server).await;
    let start = Instant::now();

    let page = web().fetch_page(&server.uri(), None).await.unwrap();

    assert!(start.elapsed() >= Duration::from_secs(1));
    assert!(!page.text.is_empty());
}

#[tokio::test]
async fn a_second_429_is_an_error() {
    let reply = ResponseTemplate::new(429).insert_header("retry-after", "0");
    let server = site(reply).await;
    assert!(web().fetch_page(&server.uri(), None).await.is_err());
    assert_eq!(server.received_requests().await.unwrap().len(), 2);
}

#[tokio::test]
async fn a_long_retry_after_is_not_waited_for() {
    let reply = ResponseTemplate::new(429).insert_header("retry-after", "3600");
    let server = site(reply).await;
    let start = Instant::now();
    assert!(web().fetch_page(&server.uri(), None).await.is_err());
    assert!(start.elapsed() < Duration::from_secs(5));
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn the_user_agent_names_the_contact_when_there_is_one() {
    let web = web();
    assert!(!web.user_agent().contains(';'));
    web.configure(Options { contact: "me@example.org".to_owned(), ..options() });
    assert!(web.user_agent().contains("kvasir"));
    assert!(web.user_agent().contains("; me@example.org)"));

    let server = site(html(article())).await;
    web.fetch_page(&server.uri(), None).await.unwrap();
    let seen = server.received_requests().await.unwrap();
    let sent = seen[0].headers.get("user-agent").unwrap().to_str().unwrap();
    assert_eq!(sent, web.user_agent());
}

#[tokio::test]
async fn other_formats_are_refused_and_a_pdf_goes_to_the_reader() {
    let refused = site(ResponseTemplate::new(200).set_body_raw("{}", "application/json")).await;
    assert!(
        web()
            .fetch_page(&refused.uri(), None)
            .await
            .unwrap_err()
            .to_string()
            .contains("not HTML or PDF")
    );

    let unknown =
        site(ResponseTemplate::new(200).set_body_raw("data", "application/octet-stream")).await;
    assert!(web().fetch_page(&unknown.uri(), None).await.is_err());

    let pdf =
        site(ResponseTemplate::new(200).set_body_raw("%PDF-1.7 ...", "application/octet-stream"))
            .await;
    let script = "cat >/dev/null; printf 'the text'";
    let web = web().with_pdf_reader("/bin/sh".into(), vec!["-c".to_owned(), script.to_owned()]);
    assert_eq!(web.fetch_page(&pdf.uri(), None).await.unwrap().text, "the text");
}

#[tokio::test]
async fn html_is_decoded_by_its_charset_and_a_body_is_cut_at_the_cap() {
    let body = format!(
        "<html><body><article><p>caf\u{e9} {}</p></article></body></html>",
        "word ".repeat(100)
    );
    let (latin, _, _) = encoding_rs::WINDOWS_1252.encode(&body);
    let server = site(
        ResponseTemplate::new(200)
            .set_body_raw(latin.into_owned(), "text/html; charset=iso-8859-1"),
    )
    .await;
    assert!(web().fetch_page(&server.uri(), None).await.unwrap().text.contains("caf\u{e9}"));

    let big = format!("%PDF-{}", "x".repeat(kvasir_fetch::MAX_BYTES + 1000));
    let server = site(ResponseTemplate::new(200).set_body_raw(big, "application/pdf")).await;
    let count = "wc -c | tr -d ' \\n'";
    let web = web().with_pdf_reader("/bin/sh".into(), vec!["-c".to_owned(), count.to_owned()]);
    let page = web.fetch_page(&server.uri(), None).await.unwrap();
    assert_eq!(page.text, kvasir_fetch::MAX_BYTES.to_string());
}

#[tokio::test]
async fn robots_txt_decides_what_may_be_read() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/robots.txt"))
        .respond_with(ResponseTemplate::new(200).set_body_string("User-agent: *\nDisallow: /no\n"))
        .expect(1)
        .mount(&server)
        .await;
    let web = web();
    assert!(web.allowed(&format!("{}/yes", server.uri())).await);
    assert!(!web.allowed(&format!("{}/no/x", server.uri())).await);
}

#[tokio::test]
async fn a_missing_robots_txt_allows_and_a_failing_one_denies() {
    let missing = site(ResponseTemplate::new(404)).await;
    assert!(web().allowed(&format!("{}/a", missing.uri())).await);
    let failing = site(ResponseTemplate::new(500)).await;
    assert!(!web().allowed(&format!("{}/a", failing.uri())).await);
}

#[tokio::test]
async fn a_robots_txt_redirect_is_followed_at_most_five_times() {
    let server = MockServer::start().await;
    Mock::given(any())
        .respond_with(ResponseTemplate::new(301).insert_header("location", "/robots.txt"))
        .mount(&server)
        .await;
    assert!(!web().allowed(&format!("{}/a", server.uri())).await);
    assert_eq!(server.received_requests().await.unwrap().len(), 6);
}
