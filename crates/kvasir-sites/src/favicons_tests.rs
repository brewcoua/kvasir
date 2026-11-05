//! Where icons come from, what is refused, and what is remembered. Sites are one local server,
//! which every name but the private ones resolves to.

use std::net::IpAddr;

use kvasir_db::rusqlite::Connection;
use kvasir_fetch::{Guard, Lookup, Options};
use tempfile::TempDir;
use tokio::io::AsyncWriteExt;
use wiremock::matchers::{any, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use super::*;

const PNG: &[u8] = b"\x89PNG\r\n\x1a\npngpngpng";
const ICO: &[u8] = b"\x00\x00\x01\x00\x01\x00icoico";
const GIF: &[u8] = b"GIF89agifgif";
const JPEG: &[u8] = b"\xff\xd8\xff\xe0jpegjpeg";
const WEBP: &[u8] = b"RIFF\x00\x00\x00\x00WEBPVP8 webpwebp";
const SVG: &[u8] = b"<svg xmlns=\"http://www.w3.org/2000/svg\"><script>alert(1)</script></svg>";

fn lookup() -> Lookup {
    Arc::new(|host: String| {
        Box::pin(async move {
            match host.as_str() {
                "intranet.example" | "internal.example" => {
                    Ok(vec!["10.0.0.1".parse::<IpAddr>().expect("an address")])
                }
                "gone.example" => Err(std::io::Error::other("no such name")),
                _ => Ok(vec!["127.0.0.1".parse::<IpAddr>().expect("an address")]),
            }
        })
    })
}

fn web(guard: Guard) -> Arc<Web> {
    let options = Options {
        contact: String::new(),
        host_interval: Duration::ZERO,
        render: false,
        stealth: false,
        max_age_hours: 48,
    };
    Arc::new(Web::new(options, 8, guard, lookup()))
}

struct Setting {
    site: MockServer,
    store: FaviconStore,
    dir: TempDir,
}

impl Setting {
    async fn new() -> Self {
        let dir = TempDir::new().unwrap();
        let store = FaviconStore::open(&dir.path().join("favicons.db")).unwrap();
        Self { site: MockServer::start().await, store, dir }
    }

    fn port(&self) -> u16 {
        self.site.address().port()
    }

    fn built(&self, providers: &[FaviconProvider]) -> Favicons {
        let settings = FaviconSettings {
            providers: providers.to_vec(),
            custom_url: format!("http://127.0.0.1:{}/{{domain}}.png", self.port()),
            ..FaviconSettings::default()
        };
        let mut icons = Favicons::new(settings, self.store.clone(), web(Guard::Loopback));
        icons.templates = Templates {
            direct: format!("http://{{domain}}:{}", self.port()),
            duckduckgo: format!("http://duck.example:{}/duck/{{domain}}.ico", self.port()),
            google: format!("http://google.example:{}/google/{{domain}}", self.port()),
        };
        icons
    }

    fn service(&self, providers: &[FaviconProvider]) -> Arc<Favicons> {
        Arc::new(self.built(providers))
    }

    async fn serve(&self, at: &str, reply: ResponseTemplate) {
        Mock::given(path(at.to_owned())).respond_with(reply).mount(&self.site).await;
    }

    /// The paths asked, leaving out robots.txt, which is not an icon's business.
    async fn asked(&self) -> Vec<String> {
        let seen = self.site.received_requests().await.unwrap();
        seen.iter()
            .map(|request| request.url.path().to_owned())
            .filter(|at| at != "/robots.txt")
            .collect()
    }

    fn age(&self, days: f64) {
        Connection::open(self.dir.path().join("favicons.db"))
            .unwrap()
            .execute("UPDATE favicons SET fetched_at = fetched_at - ?", [days * SECONDS_PER_DAY])
            .unwrap();
    }

    async fn stored(&self, host: &str) -> Option<Entry> {
        self.store.get(host, 30, 3).await.unwrap()
    }
}

fn ok(body: &[u8]) -> ResponseTemplate {
    ResponseTemplate::new(200).set_body_raw(body.to_vec(), "image/png")
}

fn page(links: &[&str]) -> ResponseTemplate {
    let body = format!("<head>{}</head>", links.concat());
    ResponseTemplate::new(200).set_body_raw(body, "text/html")
}

fn redirect(to: &str) -> ResponseTemplate {
    ResponseTemplate::new(302).insert_header("location", to)
}

fn png() -> Option<Icon> {
    Some(Icon { body: PNG.to_vec(), content_type: "image/png".to_owned() })
}

const MISS: Found = Found { icon: None, is_final: true };
const UNSURE: Found = Found { icon: None, is_final: false };

use FaviconProvider::{Custom, Direct, Duckduckgo, Google};

#[tokio::test]
async fn each_provider_is_asked_for_the_host_and_its_icon_is_kept() {
    for (provider, name, at) in [
        (Direct, "direct", "/favicon.ico"),
        (Duckduckgo, "duckduckgo", "/duck/example.org.ico"),
        (Google, "google", "/google/example.org"),
        (Custom, "custom", "/example.org.png"),
    ] {
        let setting = Setting::new().await;
        setting.serve(at, ok(PNG)).await;

        let found = setting.service(&[provider]).find("www.Example.org:8443").await;

        assert_eq!(found.icon, png(), "{name}");
        assert_eq!(setting.asked().await, [at]);
        assert_eq!(setting.stored("example.org").await.unwrap().provider.as_deref(), Some(name));
    }
}

#[test]
fn the_real_providers_are_where_they_should_be() {
    let dir = TempDir::new().unwrap();
    let store = FaviconStore::open(&dir.path().join("favicons.db")).unwrap();
    let icons = Favicons::new(FaviconSettings::default(), store, web(Guard::Public));
    let for_host = |template: &str| template.replace("{domain}", "example.org");
    assert_eq!(for_host(&icons.templates.direct), "https://example.org");
    assert_eq!(
        for_host(&icons.templates.duckduckgo),
        "https://icons.duckduckgo.com/ip3/example.org.ico"
    );
    assert_eq!(
        for_host(&icons.templates.google),
        "https://www.google.com/s2/favicons?domain=example.org&sz=64"
    );
}

#[tokio::test]
async fn the_request_names_this_service_and_asks_for_no_compression() {
    let setting = Setting::new().await;
    setting.serve("/favicon.ico", ok(PNG)).await;
    let icons = setting.service(&[Direct]);

    icons.find("example.org").await;

    let seen = setting.site.received_requests().await.unwrap();
    assert_eq!(seen[0].headers.get("user-agent").unwrap(), icons.web.user_agent().as_str());
    assert_eq!(seen[0].headers.get("accept-encoding").unwrap(), "identity");
}

#[tokio::test]
async fn providers_are_tried_in_order_until_one_answers() {
    let setting = Setting::new().await;
    setting.serve("/duck/example.org.ico", ResponseTemplate::new(404).set_body_bytes(PNG)).await;
    setting.serve("/google/example.org", ok(ICO)).await;
    setting.serve("/favicon.ico", ok(GIF)).await;

    let found = setting.service(&[Duckduckgo, Google, Direct]).find("example.org").await;

    assert_eq!(found.icon.unwrap().content_type, "image/x-icon");
    assert_eq!(setting.asked().await, ["/duck/example.org.ico", "/google/example.org"]);
}

#[tokio::test]
async fn an_answer_that_is_not_an_icon_is_a_miss_and_is_remembered() {
    for answer in [
        // DuckDuckGo and Google send a placeholder image with the 404 (probed live).
        ResponseTemplate::new(404).set_body_bytes(PNG),
        // DuckDuckGo sends an empty 200 for a site it does not know (probed live).
        ok(b""),
        ok(b"<html>no</html>"),
        ResponseTemplate::new(200).set_body_raw(SVG.to_vec(), "image/svg+xml"),
        ok(SVG),
        ok(b"\x00\x00\x02\x00\x01\x00cursor"),
    ] {
        let setting = Setting::new().await;
        setting.serve("/google/example.org", answer).await;
        let icons = setting.service(&[Google]);

        assert_eq!(icons.find("example.org").await, MISS);
        assert_eq!(icons.find("example.org").await, MISS);

        assert_eq!(setting.asked().await.len(), 1);
        assert_eq!(setting.stored("example.org").await.unwrap().icon, None);
    }
}

#[tokio::test]
async fn the_content_type_comes_from_the_bytes_not_the_site() {
    for (body, content_type) in [
        (PNG, "image/png"),
        (ICO, "image/x-icon"),
        (GIF, "image/gif"),
        (JPEG, "image/jpeg"),
        (WEBP, "image/webp"),
    ] {
        let setting = Setting::new().await;
        let reply = ResponseTemplate::new(200).set_body_raw(body.to_vec(), "text/plain");
        setting.serve("/favicon.ico", reply).await;

        let icon = setting.service(&[Direct]).find("example.org").await.icon.unwrap();

        assert_eq!((icon.body.as_slice(), icon.content_type.as_str()), (body, content_type));
    }
}

#[tokio::test]
async fn a_private_host_is_not_asked_and_is_a_final_miss() {
    let setting = Setting::new().await;

    assert_eq!(setting.service(&[Direct]).find("intranet.example").await, MISS);

    assert!(setting.asked().await.is_empty());
    assert_eq!(setting.stored("intranet.example").await.unwrap().icon, None);
}

#[tokio::test]
async fn a_redirect_to_a_private_host_is_not_followed() {
    let setting = Setting::new().await;
    let inner = format!("http://internal.example:{}/i.png", setting.port());
    setting.serve("/favicon.ico", redirect(&inner)).await;
    setting.serve("/i.png", ok(PNG)).await;

    assert_eq!(setting.service(&[Direct]).find("example.org").await.icon, None);

    assert!(!setting.asked().await.contains(&"/i.png".to_owned()));
}

#[tokio::test]
async fn a_redirect_to_a_literal_private_address_is_not_followed() {
    let setting = Setting::new().await;
    setting.serve("/favicon.ico", redirect("http://169.254.169.254/latest")).await;

    assert_eq!(setting.service(&[Direct]).find("example.org").await.icon, None);
}

#[tokio::test]
async fn a_redirect_to_another_scheme_is_not_followed() {
    let setting = Setting::new().await;
    setting.serve("/google/example.org", redirect("file:///etc/passwd")).await;

    assert_eq!(setting.service(&[Google]).find("example.org").await, MISS);
    assert_eq!(setting.asked().await, ["/google/example.org"]);
}

#[tokio::test]
async fn an_icon_after_three_redirects_is_kept_and_a_fourth_is_not_followed() {
    let setting = Setting::new().await;
    setting.serve("/google/example.org", redirect("/1")).await;
    setting.serve("/1", redirect("/2")).await;
    setting.serve("/2", redirect("/3")).await;
    setting.serve("/3", ok(GIF)).await;
    setting.serve("/google/other.org", redirect("/a")).await;
    setting.serve("/a", redirect("/b")).await;
    setting.serve("/b", redirect("/c")).await;
    setting.serve("/c", redirect("/d")).await;
    setting.serve("/d", ok(GIF)).await;
    let icons = setting.service(&[Google]);

    assert!(icons.find("example.org").await.icon.is_some());
    assert_eq!(icons.find("other.org").await.icon, None);

    assert!(!setting.asked().await.contains(&"/d".to_owned()));
}

#[tokio::test]
async fn an_icon_over_the_size_limit_is_a_miss() {
    let setting = Setting::new().await;
    setting.serve("/google/example.org", ok(&[PNG, &vec![b'x'; MAX_BYTES]].concat())).await;

    assert_eq!(setting.service(&[Google]).find("example.org").await, MISS);
}

#[tokio::test]
async fn the_custom_provider_may_be_private_but_its_redirects_may_not() {
    let setting = Setting::new().await;
    let settings = FaviconSettings {
        providers: vec![Custom],
        custom_url: format!("http://127.0.0.1:{}/{{domain}}.png", setting.port()),
        ..FaviconSettings::default()
    };
    // Held to the public internet, where 127.0.0.1 is refused to all but the operator's own.
    let icons = Arc::new(Favicons::new(settings, setting.store.clone(), web(Guard::Public)));
    setting.serve("/example.org.png", ok(PNG)).await;
    let back = format!("http://127.0.0.1:{}/secret", setting.port());
    setting.serve("/other.org.png", redirect(&back)).await;
    setting.serve("/secret", ok(PNG)).await;

    assert_eq!(icons.find("example.org").await.icon, png());
    assert_eq!(icons.find("other.org").await.icon, None);

    assert_eq!(setting.asked().await, ["/example.org.png", "/other.org.png"]);
}

#[tokio::test]
async fn a_stored_icon_is_served_without_a_request() {
    let setting = Setting::new().await;
    setting.serve("/favicon.ico", ok(PNG)).await;
    let icons = setting.service(&[Direct]);

    assert_eq!(icons.find("example.org").await.icon, png());
    assert_eq!(icons.find("www.example.org").await.icon, png());

    assert_eq!(setting.asked().await.len(), 1);
}

#[tokio::test]
async fn an_icon_and_a_miss_expire_after_their_own_ttl() {
    let setting = Setting::new().await;
    let icon = png().unwrap();
    setting.store.put("hit.org", Some(icon.clone()), Some("direct")).await.unwrap();
    setting.store.put("miss.org", None, None).await.unwrap();

    assert_eq!(setting.stored("hit.org").await.unwrap().icon, Some(icon));
    assert_eq!(setting.stored("miss.org").await.unwrap().icon, None);

    setting.age(4.0);
    assert!(setting.stored("hit.org").await.is_some());
    assert!(setting.stored("miss.org").await.is_none());

    setting.age(27.0);
    assert!(setting.stored("hit.org").await.is_none());
}

#[tokio::test]
async fn an_expired_entry_is_fetched_again_and_overwritten() {
    let setting = Setting::new().await;
    Mock::given(path("/favicon.ico"))
        .respond_with(ok(PNG))
        .up_to_n_times(1)
        .mount(&setting.site)
        .await;
    let icons = setting.service(&[Direct]);
    icons.find("example.org").await;
    setting.age(31.0);
    setting.serve("/favicon.ico", ok(GIF)).await;

    let found = icons.find("example.org").await.icon.unwrap();

    assert_eq!(found.content_type, "image/gif");
    assert_eq!(setting.asked().await.len(), 2);
    assert_eq!(setting.stored("example.org").await.unwrap().icon, Some(found));
}

#[tokio::test]
async fn flush_forgets_everything() {
    let setting = Setting::new().await;
    setting.store.put("a.org", png(), Some("direct")).await.unwrap();
    setting.store.put("b.org", None, None).await.unwrap();

    setting.service(&[Direct]).flush().await.unwrap();

    assert!(setting.stored("a.org").await.is_none());
    assert!(setting.stored("b.org").await.is_none());
    setting.store.put("a.org", png(), Some("direct")).await.unwrap();
    assert_eq!(setting.stored("a.org").await.unwrap().icon, png());
}

#[tokio::test]
async fn no_providers_means_no_request_and_no_write() {
    let setting = Setting::new().await;

    assert_eq!(setting.service(&[]).find("example.org").await, MISS);

    assert!(setting.asked().await.is_empty());
    assert!(setting.stored("example.org").await.is_none());
}

#[tokio::test]
async fn a_host_that_is_not_a_site_is_not_looked_up() {
    let setting = Setting::new().await;

    assert_eq!(setting.service(&[Direct]).find("192.168.0.1").await, MISS);

    assert!(setting.asked().await.is_empty());
    assert!(setting.stored("192.168.0.1").await.is_none());
}

#[tokio::test]
async fn concurrent_lookups_of_one_host_make_one_request() {
    let setting = Setting::new().await;
    setting.serve("/favicon.ico", ok(PNG).set_delay(Duration::from_millis(200))).await;
    let icons = setting.service(&[Direct]);

    let founds = tokio::join!(
        icons.find("example.org"),
        icons.find("example.org"),
        icons.find("www.example.org"),
        icons.find("example.org"),
    );

    for found in [founds.0, founds.1, founds.2, founds.3] {
        assert_eq!(found.icon, png());
    }
    assert_eq!(setting.asked().await.len(), 1);
    assert!(icons.flights.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_homepage_is_read_for_icon_links_when_favicon_ico_is_missing() {
    let setting = Setting::new().await;
    let cdn = format!("http://cdn.example:{}/t.png", setting.port());
    let touch = format!(r#"<link rel="apple-touch-icon" href="{cdn}" sizes="180x180">"#);
    setting
        .serve(
            "/",
            page(&[
                r#"<link rel="stylesheet" href="/s.css">"#,
                r#"<link rel="ICON" href="/small.png" sizes="16x16">"#,
                r#"<link rel="shortcut icon" href="/big.png" sizes="16x16 64x64">"#,
                &touch,
                r#"<link rel="icon" href="/vector.svg">"#,
                r#"<link rel="icon" href="/typed" type="image/svg+xml">"#,
                r#"<link rel="icon" href="data:image/png;base64,AAAA">"#,
                r#"<link rel="icon" href="javascript:alert(1)">"#,
                r#"<link rel="icon" href="/last.png">"#,
            ]),
        )
        .await;
    setting.serve("/big.png", ok(WEBP)).await;

    let found = setting.service(&[Direct]).find("example.org").await.icon.unwrap();

    assert_eq!(found.content_type, "image/webp");
    assert_eq!(setting.asked().await, ["/favicon.ico", "/", "/t.png", "/big.png"]);
}

#[tokio::test]
async fn a_base_element_moves_where_declared_icons_are() {
    let setting = Setting::new().await;
    let links = [r#"<base href="/assets/">"#, r#"<link rel="icon" href="i.png">"#];
    setting.serve("/", page(&links)).await;
    setting.serve("/assets/i.png", ok(PNG)).await;

    assert_eq!(setting.service(&[Direct]).find("example.org").await.icon, png());
}

#[tokio::test]
async fn at_most_three_declared_icons_are_tried() {
    let setting = Setting::new().await;
    let links: Vec<String> = [16, 32, 48, 64, 96]
        .map(|n| format!(r#"<link rel="icon" href="/{n}.png" sizes="{n}x{n}">"#))
        .to_vec();
    let links: Vec<&str> = links.iter().map(String::as_str).collect();
    setting.serve("/", page(&links)).await;

    assert_eq!(setting.service(&[Direct]).find("example.org").await.icon, None);

    assert_eq!(setting.asked().await[2..], ["/96.png", "/64.png", "/48.png"]);
}

#[tokio::test]
async fn a_homepage_that_robots_txt_disallows_is_not_read() {
    let setting = Setting::new().await;
    let robots = ResponseTemplate::new(200).set_body_string("User-agent: *\nDisallow: /\n");
    setting.serve("/robots.txt", robots).await;
    setting.serve("/", page(&[r#"<link rel="icon" href="/i.png">"#])).await;

    assert_eq!(setting.service(&[Direct]).find("example.org").await.icon, None);

    assert_eq!(setting.asked().await, ["/favicon.ico"]);
}

#[tokio::test]
async fn a_declared_icon_on_a_private_host_is_not_asked() {
    let setting = Setting::new().await;
    let inner =
        format!(r#"<link rel="icon" href="http://internal.example:{}/i.png">"#, setting.port());
    setting.serve("/", page(&[&inner])).await;
    setting.serve("/i.png", ok(PNG)).await;

    assert_eq!(setting.service(&[Direct]).find("example.org").await.icon, None);

    assert!(!setting.asked().await.contains(&"/i.png".to_owned()));
}

#[tokio::test]
async fn only_the_start_of_a_homepage_is_read() {
    let setting = Setting::new().await;
    let padding = format!("<!-- {} -->", "x".repeat(HOMEPAGE_BYTES));
    let body = format!(
        r#"<link rel="icon" href="/early.png">{padding}<link rel="icon" href="/late.png">"#
    );
    setting.serve("/", ResponseTemplate::new(200).set_body_raw(body, "text/html")).await;
    setting.serve("/early.png", ok(PNG)).await;

    assert_eq!(setting.service(&[Direct]).find("example.org").await.icon, png());

    assert!(!setting.asked().await.contains(&"/late.png".to_owned()));
}

#[tokio::test]
async fn a_page_nested_past_the_limit_declares_nothing() {
    let html = format!("{}<link rel=\"icon\" href=\"/i.png\">", "<div>".repeat(3000));
    assert!(icon_links(html.as_bytes(), "https://example.org/").is_empty());
}

#[tokio::test]
async fn a_transport_error_is_not_remembered() {
    let setting = Setting::new().await;
    let mut icons = setting.built(&[Direct]);
    // A port nothing listens on.
    let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    icons.templates.direct = format!("http://{{domain}}:{}", closed.local_addr().unwrap().port());
    drop(closed);
    let icons = Arc::new(icons);

    assert_eq!(icons.find("example.org").await, UNSURE);

    assert!(setting.stored("example.org").await.is_none());
}

#[tokio::test]
async fn a_provider_that_answers_makes_a_miss_final_though_another_is_down() {
    let setting = Setting::new().await;
    let mut icons = setting.built(&[Duckduckgo, Google]);
    let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    icons.templates.duckduckgo =
        format!("http://{{domain}}:{}/down", closed.local_addr().unwrap().port());
    drop(closed);
    let icons = Arc::new(icons);

    assert_eq!(icons.find("example.org").await, MISS);

    assert_eq!(setting.stored("example.org").await.unwrap().icon, None);
}

#[tokio::test]
async fn a_homepage_that_answers_makes_a_miss_final_though_favicon_ico_is_slow() {
    let setting = Setting::new().await;
    let slow = ok(PNG).set_delay(Duration::from_secs(5));
    setting.serve("/favicon.ico", slow).await;
    setting.serve("/", page(&[])).await;
    let mut icons = setting.built(&[Direct]);
    icons.attempt = Duration::from_millis(200);

    assert_eq!(Arc::new(icons).find("example.org").await, MISS);

    assert!(setting.stored("example.org").await.is_some());
}

#[tokio::test]
async fn a_store_that_fails_does_not_stop_the_lookup() {
    let setting = Setting::new().await;
    setting.serve("/favicon.ico", ok(PNG)).await;
    Connection::open(setting.dir.path().join("favicons.db"))
        .unwrap()
        .execute_batch("DROP TABLE favicons")
        .unwrap();

    assert_eq!(setting.service(&[Direct]).find("example.org").await.icon, png());
}

#[tokio::test]
async fn a_lookup_past_its_deadline_stops_and_remembers_nothing() {
    let setting = Setting::new().await;
    setting.serve("/duck/example.org.ico", ok(PNG).set_delay(Duration::from_secs(5))).await;
    setting.serve("/google/example.org", ok(PNG)).await;
    let mut icons = setting.built(&[Duckduckgo, Google]);
    icons.deadline = Duration::from_millis(200);
    let icons = Arc::new(icons);

    assert_eq!(icons.find("example.org").await, UNSURE);
    // The caller's wait and the lookup's clock end together, so let the lookup see its own.
    tokio::time::sleep(Duration::from_millis(100)).await;

    assert_eq!(setting.asked().await, ["/duck/example.org.ico"]);
    assert!(setting.stored("example.org").await.is_none());
}

#[tokio::test]
async fn an_icon_still_arriving_at_its_attempts_end_is_not_kept() {
    // Sends half an image, then holds the connection open.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let head = format!("HTTP/1.1 200 OK\r\ncontent-length: {}\r\n\r\n", PNG.len() * 2);
        stream.write_all(head.as_bytes()).await.unwrap();
        stream.write_all(PNG).await.unwrap();
        stream.flush().await.unwrap();
        tokio::time::sleep(Duration::from_secs(5)).await;
    });
    let setting = Setting::new().await;
    let mut icons = setting.built(&[Google]);
    icons.attempt = Duration::from_millis(300);
    icons.templates.google = format!("http://{{domain}}:{port}/i");

    assert_eq!(Arc::new(icons).find("example.org").await.icon, None);

    server.abort();
}

#[tokio::test]
async fn an_icon_that_comes_compressed_anyway_is_not_read() {
    let setting = Setting::new().await;
    setting.serve("/google/example.org", ok(PNG).insert_header("content-encoding", "gzip")).await;

    assert_eq!(setting.service(&[Google]).find("example.org").await.icon, None);
}

#[tokio::test]
async fn a_name_that_does_not_resolve_is_not_remembered_as_a_miss() {
    let setting = Setting::new().await;

    assert_eq!(setting.service(&[Direct]).find("gone.example").await, UNSURE);

    assert!(setting.stored("gone.example").await.is_none());
}

#[tokio::test]
async fn a_name_slow_to_resolve_does_not_hold_the_caller_past_the_deadline() {
    let setting = Setting::new().await;
    let settings = FaviconSettings { providers: vec![Direct], ..FaviconSettings::default() };
    let stuck: Lookup = Arc::new(|_| {
        Box::pin(async {
            tokio::time::sleep(Duration::from_secs(30)).await;
            Ok(Vec::new())
        })
    });
    let options = Options {
        contact: String::new(),
        host_interval: Duration::ZERO,
        render: false,
        stealth: false,
        max_age_hours: 48,
    };
    let web = Arc::new(Web::new(options, 8, Guard::Loopback, stuck));
    let mut icons = Favicons::new(settings, setting.store.clone(), web);
    icons.deadline = Duration::from_millis(50);
    let start = Instant::now();

    assert_eq!(Arc::new(icons).find("example.org").await, UNSURE);

    assert!(start.elapsed() < Duration::from_secs(5));
}

#[tokio::test]
async fn a_lookup_past_the_limit_in_progress_is_refused_without_a_request() {
    let setting = Setting::new().await;
    Mock::given(any()).respond_with(ok(PNG)).mount(&setting.site).await;
    let mut icons = setting.built(&[Direct]);
    icons.max_flights = 0;

    assert_eq!(Arc::new(icons).find("example.org").await, UNSURE);

    assert!(setting.asked().await.is_empty());
}
