//! The order a host resolves in, and what is asked of Wikidata and kept.

use std::time::SystemTime;

use kvasir_fetch::{Guard, Options, system_lookup};
use tempfile::TempDir;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

use super::wikidata::{INTERNATIONAL, MAX_BACKOFF, NAMESPACE};
use super::*;
use crate::SECONDS_PER_DAY;
use kvasir_types::sites::ListChange;

const WAIT: Duration = Duration::from_secs(5);
const NEWSPAPER: &str = "Q11032";
const ORG: &str = "Q43229";
const STANDARDS: &str = "Q1328899";
const NGO: &str = "Q79913";
const WIKI: &str = "Q171";
const AI: &str = "Q125361038";
const ENCYCLOPEDIA: &str = "Q5292";
const COUNTRY: &str = "Q6256";

type Items = Vec<(&'static str, &'static str, Vec<&'static str>)>;

/// A fake query service: answers rows by host as WDQS JSON, and keeps the queries it was sent.
#[derive(Clone, Default)]
struct Wdqs {
    rows: Vec<(&'static str, Items)>,
    // The name a host is listed under on Wikidata, when not its own.
    names: Vec<(&'static str, &'static str)>,
    status: Option<u16>,
    retry_after: Option<&'static str>,
    delay: Duration,
    queries: Arc<Mutex<Vec<String>>>,
}

impl Wdqs {
    fn queries(&self) -> Vec<String> {
        self.queries.lock().unwrap().clone()
    }
}

impl Respond for Wdqs {
    fn respond(&self, request: &Request) -> ResponseTemplate {
        let query = url::form_urlencoded::parse(&request.body)
            .find(|(key, _)| key == "query")
            .map(|(_, query)| query.into_owned())
            .unwrap_or_default();
        self.queries.lock().unwrap().push(query.clone());
        if let Some(status) = self.status {
            let reply = ResponseTemplate::new(status);
            return match self.retry_after {
                Some(after) => reply.insert_header("retry-after", after),
                None => reply,
            };
        }
        let mut bindings = Vec::new();
        for (host, items) in &self.rows {
            let name = self.names.iter().find(|(h, _)| h == host).map_or(*host, |(_, name)| *name);
            if !query.contains(&format!("(\"{host}\" \"{name}\")")) {
                continue;
            }
            for (item, label, roots) in items {
                for root in roots {
                    let mut row = serde_json::json!({
                        "h": {"type": "literal", "value": host},
                        "i": {"type": "uri", "value": format!("{NAMESPACE}{item}")},
                        "root": {"type": "uri", "value": format!("{NAMESPACE}{root}")},
                    });
                    if !label.is_empty() {
                        row["l"] = serde_json::json!({"value": label});
                    }
                    bindings.push(row);
                }
            }
        }
        ResponseTemplate::new(200)
            .set_delay(self.delay)
            .set_body_json(serde_json::json!({"head": {}, "results": {"bindings": bindings}}))
    }
}

/// The one list on as it ships is on the real web, where no test here goes.
fn turn_off_shipped_list(dir: &std::path::Path) {
    kvasir_db::Key::for_tests()
        .connect(dir.join("sources.db"))
        .unwrap()
        .execute("INSERT INTO lists VALUES ('dotgov', '{\"enabled\":false}')", [])
        .unwrap();
}

struct Setting {
    server: MockServer,
    store: SourceStore,
    dir: TempDir,
}

impl Setting {
    async fn new() -> Self {
        let dir = TempDir::new().unwrap();
        let store = SourceStore::open(&dir.path().join("sources.db"), &kvasir_db::Key::for_tests())
            .unwrap();
        turn_off_shipped_list(dir.path());
        Self { server: MockServer::start().await, store, dir }
    }

    async fn wdqs(&self, wdqs: Wdqs) -> Wdqs {
        Mock::given(method("POST"))
            .and(path("/sparql"))
            .respond_with(wdqs.clone())
            .mount(&self.server)
            .await;
        wdqs
    }

    fn settings(&self) -> SourceSettings {
        SourceSettings {
            enabled: true,
            endpoint: format!("{}/sparql", self.server.uri()),
            lists: Vec::new(),
            ..SourceSettings::default()
        }
    }

    fn built(&self, settings: SourceSettings) -> Sources {
        let options = Options {
            contact: "me@example.org".to_owned(),
            host_interval: Duration::ZERO,
            render: false,
            stealth: false,
            max_age_hours: 48,
        };
        let web = Arc::new(Web::new(options, 8, Guard::Public, system_lookup()));
        let mut sources =
            Sources::new(settings, self.store.clone(), self.dir.path().to_owned(), web);
        sources.rsp_url = format!("{}/rsp", self.server.uri());
        sources
    }

    fn service(&self) -> Arc<Sources> {
        Arc::new(self.built(self.settings()))
    }

    fn with(&self, settings: SourceSettings) -> Arc<Sources> {
        Arc::new(self.built(settings))
    }

    fn age(&self, days: f64) {
        kvasir_db::Key::for_tests()
            .connect(self.dir.path().join("sources.db"))
            .unwrap()
            .execute("UPDATE sources SET fetched_at = fetched_at - ?", [days * SECONDS_PER_DAY])
            .unwrap();
    }

    async fn stored(&self, host: &str) -> Option<Option<Match>> {
        self.store.get(host, 30, 7).await.unwrap()
    }

    async fn list(&self, at: &str, reply: ResponseTemplate) -> String {
        Mock::given(path(at.to_owned())).respond_with(reply).mount(&self.server).await;
        format!("{}{at}", self.server.uri())
    }

    fn list_files(&self) -> usize {
        std::fs::read_dir(self.dir.path())
            .unwrap()
            .filter(|file| {
                let name = file.as_ref().unwrap().file_name();
                name.to_string_lossy().starts_with("sources-")
            })
            .count()
    }
}

async fn one(sources: &Arc<Sources>, host: &str, deadline: Duration) -> Source {
    sources.classify(&[host.to_owned()], deadline).await.remove(host).unwrap()
}

fn tags(source: &Source) -> Vec<&str> {
    source.tags.iter().map(String::as_str).collect()
}

async fn settle() {
    tokio::time::sleep(Duration::from_millis(200)).await;
}

#[tokio::test]
async fn suffix_rules_set_the_kind_without_waiting_for_wikidata() {
    for (host, kind) in [
        ("who.int", "intergovernmental"),
        ("commission.europa.eu", "intergovernmental"),
        ("cdc.gov", "government"),
        ("army.mil", "government"),
        ("education.gouv.fr", "government"),
        ("service.gov.uk", "government"),
        ("canada.gc.ca", "government"),
        ("mit.edu", "academic"),
        ("ox.ac.uk", "academic"),
        ("unsw.edu.au", "academic"),
    ] {
        let setting = Setting::new().await;
        setting.wdqs(Wdqs { delay: Duration::from_secs(10), ..Wdqs::default() }).await;
        let start = Instant::now();

        let found = one(&setting.service(), host, WAIT).await;

        assert!(start.elapsed() < Duration::from_secs(1), "{host}");
        assert_eq!((found.kind.as_str(), found.origin.as_str()), (kind, "rule"), "{host}");
        assert_eq!(found.tier(), Tier::High, "{host}");
    }
}

#[tokio::test]
async fn wikidata_cannot_change_the_kind_a_rule_set() {
    // europa.eu is the Health-EU portal on Wikidata, an organization; the suffix rule wins.
    let setting = Setting::new().await;
    let rows = vec![("europa.eu", vec![("Q684245", "Health-EU portal", vec![ORG])])];
    setting.wdqs(Wdqs { rows, ..Wdqs::default() }).await;
    let sources = setting.service();
    one(&sources, "europa.eu", WAIT).await;
    settle().await;

    let found = one(&sources, "europa.eu", WAIT).await;

    assert_eq!((found.kind.as_str(), found.origin.as_str()), ("intergovernmental", "rule"));
    assert_eq!((found.label.as_str(), found.qid.as_str()), ("Health-EU portal", "Q684245"));
}

#[tokio::test]
async fn iso_is_standards_though_it_reaches_international_organization() {
    let setting = Setting::new().await;
    let rows = vec![("iso.org", vec![("Q15028", "ISO", vec![STANDARDS, INTERNATIONAL, NGO, ORG])])];
    setting.wdqs(Wdqs { rows, ..Wdqs::default() }).await;

    let found = one(&setting.service(), "iso.org", WAIT).await;

    assert_eq!(
        (found.kind.as_str(), found.qid.as_str(), found.label.as_str()),
        ("standards", "Q15028", "ISO")
    );
    assert!(found.tags.contains("official"));
}

#[tokio::test]
async fn a_catalog_style_list_can_give_its_sites_a_kind() {
    let setting = Setting::new().await;
    let sources = setting.service();
    sources
        .add_list(ListChange {
            name: Some("Official".to_owned()),
            entries: Some(vec!["agency.example".to_owned()]),
            effect: Some("kind:government".to_owned()),
            ..<_>::default()
        })
        .await
        .unwrap();

    let found = one(&sources, "agency.example", WAIT).await;

    assert_eq!((found.kind.as_str(), found.origin.as_str()), ("government", "list"));
}

#[tokio::test]
async fn who_is_intergovernmental_and_a_country_is_government() {
    let setting = Setting::new().await;
    let rows = vec![
        ("example-who.org", vec![("Q7817", "WHO", vec![INTERNATIONAL, ORG])]),
        ("natural-resources.canada.ca", vec![("Q16", "Canada", vec![COUNTRY, ORG])]),
    ];
    let names = vec![("natural-resources.canada.ca", "canada.ca")];
    setting.wdqs(Wdqs { rows, names, ..Wdqs::default() }).await;
    let sources = setting.service();

    assert_eq!(one(&sources, "example-who.org", WAIT).await.kind, "intergovernmental");
    let found = one(&sources, "natural-resources.canada.ca", WAIT).await;
    assert_eq!(
        (found.kind.as_str(), found.tier(), found.qid.as_str()),
        ("government", Tier::High, "Q16")
    );
}

#[tokio::test]
async fn a_host_matching_several_items_takes_the_best_kind_and_all_tags() {
    let setting = Setting::new().await;
    let rows = vec![(
        "nytimes.com",
        vec![
            ("Q9684", "The New York Times", vec![NEWSPAPER, ORG]),
            ("Q999", "nytimes.com", vec![ORG, WIKI]),
        ],
    )];
    setting.wdqs(Wdqs { rows, ..Wdqs::default() }).await;

    let found = one(&setting.service(), "nytimes.com", WAIT).await;

    assert_eq!((found.kind.as_str(), found.qid.as_str()), ("news", "Q9684"));
    assert_eq!(found.label, "The New York Times");
    assert_eq!(tags(&found), ["community", "official"]);
}

#[tokio::test]
async fn a_subdomain_is_asked_with_its_registrable_domain() {
    let setting = Setting::new().await;
    let rows = vec![("en.wikipedia.org", vec![("Q328", "Wikipedia", vec![ENCYCLOPEDIA, WIKI])])];
    let names = vec![("en.wikipedia.org", "wikipedia.org")];
    let asked = setting.wdqs(Wdqs { rows, names, ..Wdqs::default() }).await;

    let found = one(&setting.service(), "en.wikipedia.org", WAIT).await;

    let queries = asked.queries();
    assert!(queries[0].contains("(\"en.wikipedia.org\" \"en.wikipedia.org\")"));
    assert!(!queries[0].contains("(\"en.wikipedia.org\" \"wikipedia.org\")"));
    assert!(queries[1].contains("(\"en.wikipedia.org\" \"wikipedia.org\")"));
    assert!(queries[1].contains("(\"en.wikipedia.org\" <https://www.wikipedia.org/>)"));
    assert_eq!((found.kind.as_str(), found.tier()), ("reference", Tier::Normal));
    assert!(found.tags.contains("community"));
}

#[tokio::test]
async fn a_subdomain_with_its_own_item_does_not_take_its_parents() {
    let setting = Setting::new().await;
    let rows = vec![
        ("docs.example.org", vec![("Q2", "Docs", vec![ENCYCLOPEDIA])]),
        ("example.org", vec![("Q1", "Example", vec![ORG])]),
    ];
    let asked = setting.wdqs(Wdqs { rows, ..Wdqs::default() }).await;

    let found = one(&setting.service(), "docs.example.org", WAIT).await;

    assert_eq!((found.kind.as_str(), found.qid.as_str()), ("reference", "Q2"));
    assert_eq!(asked.queries().len(), 1);
}

#[tokio::test]
async fn international_organization_is_decided_per_item() {
    let setting = Setting::new().await;
    let rows = vec![(
        "mixed.example",
        vec![("Q1", "A NGO", vec![INTERNATIONAL, NGO]), ("Q2", "An IGO", vec![INTERNATIONAL])],
    )];
    setting.wdqs(Wdqs { rows, ..Wdqs::default() }).await;

    let found = one(&setting.service(), "mixed.example", WAIT).await;

    assert_eq!((found.kind.as_str(), found.qid.as_str()), ("intergovernmental", "Q2"));
}

#[tokio::test]
async fn each_name_is_asked_as_eight_urls_and_the_request_names_this_service() {
    let setting = Setting::new().await;
    let asked = setting.wdqs(Wdqs::default()).await;
    let sources = setting.service();

    one(&sources, "reintech.io", WAIT).await;

    assert_eq!(asked.queries()[0].matches("(\"reintech.io\" <").count(), 8);
    let seen = setting.server.received_requests().await.unwrap();
    assert_eq!(seen[0].headers.get("user-agent").unwrap(), sources.web.user_agent().as_str());
    assert_eq!(seen[0].headers.get("accept").unwrap(), "application/sparql-results+json");
}

#[tokio::test]
async fn a_wikidata_ai_site_is_avoid() {
    let setting = Setting::new().await;
    let rows = vec![("grokipedia.example", vec![("Q1", "Grokipedia", vec![ENCYCLOPEDIA, AI])])];
    setting.wdqs(Wdqs { rows, ..Wdqs::default() }).await;

    assert_eq!(one(&setting.service(), "grokipedia.example", WAIT).await.tier(), Tier::Avoid);
}

#[tokio::test]
async fn an_unknown_host_is_unknown_and_remembered_as_a_miss() {
    let setting = Setting::new().await;
    let asked = setting.wdqs(Wdqs::default()).await;
    let sources = setting.service();

    let found = one(&sources, "reintech.io", WAIT).await;
    assert_eq!(found, Source::new("reintech.io", "unknown", &[], ""));
    settle().await;
    one(&sources, "reintech.io", WAIT).await;

    assert_eq!(asked.queries().len(), 1);
    assert_eq!(setting.stored("reintech.io").await, Some(None));
}

#[tokio::test]
async fn hosts_are_normalized_and_an_ip_is_unknown() {
    let setting = Setting::new().await;
    let asked = setting.wdqs(Wdqs::default()).await;
    let raw = ["WWW.Reintech.io:443".to_owned(), "10.0.0.1".to_owned()];

    let found = setting.service().classify(&raw, WAIT).await;

    assert_eq!(found["WWW.Reintech.io:443"].host, "reintech.io");
    assert_eq!(found["10.0.0.1"].kind, "unknown");
    assert!(!asked.queries()[0].contains("10.0.0.1"));
}

#[tokio::test]
async fn hosts_are_asked_in_batches_of_fifty() {
    let setting = Setting::new().await;
    let asked = setting.wdqs(Wdqs::default()).await;
    let hosts: Vec<String> = (0..120).map(|n| format!("site{n}.example")).collect();

    setting.service().classify(&hosts, WAIT).await;

    assert_eq!(asked.queries().len(), 3);
}

#[tokio::test]
async fn two_asks_for_a_host_send_one_query() {
    let setting = Setting::new().await;
    let rows = vec![("nytimes.com", vec![("Q9684", "NYT", vec![NEWSPAPER])])];
    let delay = Duration::from_millis(200);
    let asked = setting.wdqs(Wdqs { rows, delay, ..Wdqs::default() }).await;
    let sources = setting.service();

    let (first, second) =
        tokio::join!(one(&sources, "nytimes.com", WAIT), one(&sources, "nytimes.com", WAIT));

    assert_eq!(asked.queries().len(), 1);
    assert_eq!((first.kind.as_str(), second.kind.as_str()), ("news", "news"));
    assert!(sources.flights.lock().await.is_empty());
}

#[tokio::test]
async fn past_the_deadline_a_host_is_unknown_and_known_by_the_next_ask() {
    let setting = Setting::new().await;
    let rows = vec![("nytimes.com", vec![("Q9684", "NYT", vec![NEWSPAPER])])];
    let delay = Duration::from_millis(300);
    setting.wdqs(Wdqs { rows, delay, ..Wdqs::default() }).await;
    let sources = setting.service();
    let short = Duration::from_millis(50);

    assert_eq!(one(&sources, "nytimes.com", short).await.kind, "unknown");
    tokio::time::sleep(Duration::from_millis(500)).await;

    assert_eq!(one(&sources, "nytimes.com", short).await.kind, "news");
}

#[tokio::test]
async fn a_rate_limit_stores_no_miss_and_backs_off_by_retry_after() {
    let setting = Setting::new().await;
    let wdqs = Wdqs { status: Some(429), retry_after: Some("120"), ..Wdqs::default() };
    let asked = setting.wdqs(wdqs).await;
    let sources = setting.service();

    assert_eq!(one(&sources, "nytimes.com", WAIT).await.kind, "unknown");
    assert_eq!(setting.stored("nytimes.com").await, None);
    one(&sources, "reintech.io", WAIT).await;

    assert_eq!(asked.queries().len(), 1);
    let until = sources.backoff_until.lock().unwrap().unwrap();
    assert!((100..=120).contains(&(until - Instant::now()).as_secs()));
}

#[tokio::test]
async fn a_retry_after_that_is_not_a_sane_number_is_clamped() {
    for retry_after in ["nan", "inf", "-5", "soon"] {
        let setting = Setting::new().await;
        let wdqs = Wdqs { status: Some(429), retry_after: Some(retry_after), ..Wdqs::default() };
        setting.wdqs(wdqs).await;
        let sources = setting.service();

        one(&sources, "nytimes.com", WAIT).await;

        let until = sources.backoff_until.lock().unwrap().unwrap();
        assert!(until.saturating_duration_since(Instant::now()).as_secs_f64() <= MAX_BACKOFF);
    }
}

#[tokio::test]
async fn a_failed_query_stores_no_miss_and_a_server_error_backs_off_briefly() {
    let setting = Setting::new().await;
    let mut settings = setting.settings();
    let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    settings.endpoint = format!("http://127.0.0.1:{}/", closed.local_addr().unwrap().port());
    drop(closed);
    assert_eq!(one(&setting.with(settings), "nytimes.com", WAIT).await.kind, "unknown");
    assert_eq!(setting.stored("nytimes.com").await, None);

    let asked = setting.wdqs(Wdqs { status: Some(503), ..Wdqs::default() }).await;
    let sources = setting.service();
    one(&sources, "nytimes.com", WAIT).await;
    one(&sources, "reintech.io", WAIT).await;
    assert_eq!(asked.queries().len(), 1);
}

#[tokio::test]
async fn a_match_expires_after_ttl_days_and_a_miss_after_miss_ttl_days() {
    let setting = Setting::new().await;
    let rows = vec![("nytimes.com", vec![("Q9684", "NYT", vec![NEWSPAPER])])];
    let asked = setting.wdqs(Wdqs { rows, ..Wdqs::default() }).await;
    let sources = setting.service();
    one(&sources, "nytimes.com", WAIT).await;
    one(&sources, "reintech.io", WAIT).await;
    settle().await;

    setting.age(8.0);
    assert!(setting.stored("nytimes.com").await.is_some());
    assert!(setting.stored("reintech.io").await.is_none());
    setting.age(23.0);
    assert!(setting.stored("nytimes.com").await.is_none());

    one(&sources, "nytimes.com", WAIT).await;
    assert_eq!(asked.queries().len(), 3);
}

#[tokio::test]
async fn with_wikidata_off_nothing_is_asked_or_read_from_the_cache() {
    let setting = Setting::new().await;
    let asked = setting.wdqs(Wdqs::default()).await;
    let stored = Match {
        kind: "news".to_owned(),
        tags: BTreeSet::new(),
        label: String::new(),
        qid: String::new(),
    };
    setting.store.put("nytimes.com", Some(stored)).await.unwrap();
    let sources = setting.with(SourceSettings { wikidata: false, ..setting.settings() });

    assert_eq!(one(&sources, "nytimes.com", WAIT).await.kind, "unknown");
    assert_eq!(one(&sources, "cdc.gov", WAIT).await.kind, "government");
    assert!(asked.queries().is_empty());
}

#[tokio::test]
async fn a_mark_sets_the_kind_or_the_tier() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let sources = setting.service();
    sources.mark("www.Reintech.io", "deny").await.unwrap();
    sources.mark("blog.example", "news").await.unwrap();

    assert_eq!(one(&sources, "reintech.io", WAIT).await.tier(), Tier::Avoid);
    assert_eq!(one(&sources, "docs.reintech.io", WAIT).await.origin, "mark");
    let blog = one(&sources, "blog.example", WAIT).await;
    assert_eq!((blog.kind.as_str(), blog.origin.as_str()), ("news", "mark"));
    assert!(sources.marked("reintech.io").await.unwrap());
    sources.unmark("reintech.io").await.unwrap();
    assert_eq!(one(&sources, "reintech.io", WAIT).await.origin, "");
    assert!(!sources.marked("reintech.io").await.unwrap());
}

#[tokio::test]
async fn a_mark_must_be_allow_deny_or_a_kind() {
    let setting = Setting::new().await;
    let sources = setting.service();
    for (host, value) in
        [("reintech.io", "trusted"), ("reintech.io", "unknown"), ("10.0.0.1", "allow")]
    {
        let refused = sources.mark(host, value).await;
        assert!(matches!(refused, Err(MarkError::Invalid { .. })), "{host} {value}");
    }
}

#[tokio::test]
async fn a_mark_beats_the_rules_and_the_lists() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let sources = setting.service();
    sources.mark("cdc.gov", "deny").await.unwrap();
    sources.mark("deepwiki.com", "allow").await.unwrap();

    assert_eq!(one(&sources, "cdc.gov", WAIT).await.tier(), Tier::Avoid);
    assert_eq!(one(&sources, "deepwiki.com", WAIT).await.tier(), Tier::High);
}

#[tokio::test]
async fn config_deny_beats_a_mark_and_allow_beats_a_list() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let sources = setting.with(SourceSettings {
        deny: vec!["Reintech.io".to_owned()],
        allow: vec!["grokipedia.com".to_owned()],
        ..setting.settings()
    });
    sources.mark("reintech.io", "allow").await.unwrap();

    let found = one(&sources, "docs.reintech.io", WAIT).await;
    assert_eq!((found.tier(), found.origin.as_str()), (Tier::Avoid, "config"));
    assert_eq!(one(&sources, "grokipedia.com", WAIT).await.tier(), Tier::High);
    assert!(sources.locked("docs.reintech.io"));
    assert!(!sources.locked("other.example"));
}

#[tokio::test]
async fn config_deny_beats_config_allow() {
    let setting = Setting::new().await;
    let sources = setting.with(SourceSettings {
        allow: vec!["a.example".to_owned()],
        deny: vec!["a.example".to_owned()],
        ..setting.settings()
    });
    assert_eq!(one(&sources, "a.example", WAIT).await.tier(), Tier::Avoid);
}

#[tokio::test]
async fn a_mark_survives_cache_expiry() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let first = setting.service();
    first.mark("nytimes.com", "deny").await.unwrap();
    first.mark("reintech.io", "academic").await.unwrap();
    one(&first, "other.example", WAIT).await;
    settle().await;
    setting.age(365.0);

    let later = setting.service();

    assert_eq!(one(&later, "nytimes.com", Duration::ZERO).await.tier(), Tier::Avoid);
    assert_eq!(one(&later, "reintech.io", Duration::ZERO).await.kind, "academic");
    assert!(setting.stored("other.example").await.is_none());
}

#[tokio::test]
async fn a_fully_qualified_name_with_a_trailing_dot_is_the_same_host() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let sources = setting
        .with(SourceSettings { deny: vec!["farm.example.".to_owned()], ..setting.settings() });

    assert_eq!(one(&sources, "deepwiki.com.", WAIT).await.tier(), Tier::Avoid);
    assert_eq!(one(&sources, "farm.example.", WAIT).await.origin, "config");
    sources.mark("blog.example.", "news").await.unwrap();
    assert_eq!(one(&sources, "blog.example", WAIT).await.kind, "news");
}

#[tokio::test]
async fn the_bundled_list_marks_ai_generated_sites_and_their_subdomains() {
    let setting = Setting::new().await;
    let asked = setting.wdqs(Wdqs::default()).await;
    let sources = setting.service();

    assert_eq!(one(&sources, "deepwiki.com", WAIT).await.tier(), Tier::Avoid);
    assert_eq!(one(&sources, "foo.grokipedia.com", WAIT).await.tier(), Tier::Avoid);
    assert_eq!(one(&sources, "deepwiki.com", WAIT).await.origin, "list");
    assert!(asked.queries().is_empty());
}

const HOSTS_FILE: &str = "# a comment\n\n0.0.0.0 forkful.ai\r\n127.0.0.1\tcgdream.ai\r\nlocalhost\n\
    0.0.0.0 tryleap.ai # ads\n";
const UBLACKLIST: &str = "### a comment\n*://*.otter.ai/*\n*://krisp.ai/*\n/pinterest.+\\/racingtff/\n\
    *://example.org/some/path/*\n";
const CISA: &str = "Domain name,Domain type,Organization name\n29palms.gov,City,City of Twentynine \
    Palms\nAbbeville.GOV,City,\"Abbeville, LA\"\n";
const ADBLOCK: &str = "! Title: x\ndomain.example##a[href*=\"y\"]\n||Ads.example^\n||b.example^$all\n\
    ||c.example^$document\n||d.example^$third-party\n||e.example/path^\n";

#[test]
fn lists_are_read_as_hosts_ublacklist_or_csv() {
    for (text, want) in [
        (HOSTS_FILE, &["cgdream.ai", "forkful.ai", "tryleap.ai"][..]),
        (UBLACKLIST, &["krisp.ai", "otter.ai"]),
        (CISA, &["29palms.gov", "abbeville.gov"]),
        (ADBLOCK, &["ads.example", "b.example", "c.example"]),
    ] {
        let mut got: Vec<String> = parse_list(text).hosts.into_iter().collect();
        got.sort();
        assert_eq!(got, want);
    }
}

#[test]
fn a_list_may_end_its_lines_in_a_lone_carriage_return() {
    let got = parse_list("a.example\rb.example\r").hosts;
    assert_eq!(got, HashSet::from(["a.example".to_owned(), "b.example".to_owned()]));
}

#[tokio::test]
async fn a_catalog_list_keeps_what_was_changed_until_removed() {
    let setting = Setting::new().await;
    let sources = setting.service();

    let change =
        ListChange { enabled: Some(true), effect: Some("demote".to_owned()), ..<_>::default() };
    let changed = sources.change_list("seo-garbage", change).await.unwrap();
    sources
        .change_list(
            "seo-garbage",
            ListChange { except: Some(vec!["WWW.A.org".to_owned()]), ..<_>::default() },
        )
        .await
        .unwrap();

    assert!(changed.list.enabled);
    let later = setting.service().lists().await.unwrap();
    let list = later.iter().find(|list| list.id == "seo-garbage").unwrap();
    assert_eq!((list.enabled, list.effect.as_str()), (true, "demote"));
    assert_eq!(list.except, ["a.org"]);
    assert_eq!(later.len(), catalog().len());

    sources.remove_list("seo-garbage").await.unwrap();
    let back = sources.lists().await.unwrap();
    let list = back.iter().find(|list| list.id == "seo-garbage").unwrap();
    assert_eq!((list.enabled, list.effect.as_str()), (false, "block"));
}

#[tokio::test]
async fn a_catalog_list_keeps_its_source_and_a_missing_list_is_refused() {
    let setting = Setting::new().await;
    let sources = setting.service();

    let moved = ListChange { url: Some("https://l.example/a".to_owned()), ..<_>::default() };
    assert!(matches!(sources.change_list("seo-garbage", moved).await, Err(ListError::Invalid(_))));
    assert!(matches!(
        sources.change_list("nope", ListChange::default()).await,
        Err(ListError::Missing(_))
    ));
    assert!(matches!(sources.remove_list("nope").await, Err(ListError::Missing(_))));
}

#[tokio::test]
async fn an_own_list_is_kept_changed_and_removed() {
    let setting = Setting::new().await;
    let sources = setting.service();

    let added = sources
        .add_list(ListChange {
            name: Some("Mine".to_owned()),
            entries: Some(vec!["Farm.org".to_owned()]),
            ..<_>::default()
        })
        .await
        .unwrap();
    assert!(added.list.custom && added.list.enabled && added.list.id.starts_with("custom-"));
    assert_eq!(added.hosts, 1);
    assert_eq!(added.list.effect, "block");

    let change = ListChange { effect: Some("exempt".to_owned()), ..<_>::default() };
    sources.change_list(&added.list.id, change).await.unwrap();
    let lists = setting.service().lists().await.unwrap();
    assert_eq!(lists.last().unwrap().effect, "exempt");

    sources.remove_list(&added.list.id).await.unwrap();
    assert_eq!(sources.lists().await.unwrap().len(), catalog().len());
    let nameless = ListChange { entries: Some(vec!["a.org".to_owned()]), ..<_>::default() };
    assert!(matches!(sources.add_list(nameless).await, Err(ListError::Invalid(_))));
}

/// An own list of `effect` holding `entries`, in effect when this returns.
async fn own(sources: &Arc<Sources>, effect: &str, entries: &[&str]) -> SourceList {
    let change = ListChange {
        name: Some(format!("{effect} list")),
        entries: Some(entries.iter().map(|entry| (*entry).to_owned()).collect()),
        effect: Some(effect.to_owned()),
        ..ListChange::default()
    };
    sources.add_list(change).await.unwrap().list
}

#[tokio::test]
async fn a_block_list_decides_alone_and_names_itself() {
    let setting = Setting::new().await;
    let asked = setting.wdqs(Wdqs::default()).await;
    let sources = setting.service();
    own(&sources, "block", &["farm.org"]).await;

    let found = one(&sources, "blog.farm.org", WAIT).await;
    assert_eq!(found.tier(), Tier::Avoid);
    assert_eq!(found.origin, "list");
    assert_eq!(found.listed_by, ["block list"]);
    assert!(asked.queries().is_empty(), "a blocked site is not looked up");
}

#[tokio::test]
async fn a_demotion_joins_what_the_rest_of_the_order_finds() {
    let setting = Setting::new().await;
    let rows = vec![("nytimes.example", vec![("Q1", "NYT", vec![NEWSPAPER])])];
    setting.wdqs(Wdqs { rows, ..Wdqs::default() }).await;
    let sources = setting.service();
    own(&sources, "demote", &["nytimes.example", "plain.org"]).await;

    let known = one(&sources, "nytimes.example", WAIT).await;
    assert_eq!(known.kind, "news");
    assert_eq!(known.tier(), Tier::Low);
    assert_eq!(known.listed_by, ["demote list"]);
    let plain = one(&sources, "plain.org", WAIT).await;
    assert_eq!((plain.tier(), plain.origin.as_str()), (Tier::Low, "list"));
}

#[tokio::test]
async fn exempting_cancels_a_block_and_a_demotion_and_grants_no_trust() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let sources = setting.service();
    own(&sources, "block", &["marked.org", "listed.org", "blocked.org"]).await;
    own(&sources, "demote", &["marked.org", "listed.org"]).await;
    own(&sources, "exempt", &["listed.org"]).await;
    sources.mark("marked.org", "exempt").await.unwrap();

    for host in ["marked.org", "listed.org"] {
        let found = one(&sources, host, WAIT).await;
        assert_eq!(found.tier(), Tier::Unknown, "{host}");
        assert!(found.listed_by.is_empty(), "{host}");
    }
    assert!(one(&sources, "marked.org", WAIT).await.tags.contains("exempt"));
    assert_eq!(one(&sources, "blocked.org", WAIT).await.tier(), Tier::Avoid);
}

#[tokio::test]
async fn exempting_cancels_the_bundled_list_and_keeps_a_kind_list() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let sources = setting.service();
    let bundled = sources.bundled.keys().next().unwrap().clone();
    assert_eq!(one(&sources, &bundled, WAIT).await.tier(), Tier::Avoid);

    sources.mark(&bundled, "exempt").await.unwrap();
    assert_eq!(one(&sources, &bundled, WAIT).await.tier(), Tier::Unknown);

    own(&sources, "kind:news", &["paper.org"]).await;
    own(&sources, "block", &["paper.org"]).await;
    sources.mark("paper.org", "exempt").await.unwrap();
    let paper = one(&sources, "paper.org", WAIT).await;
    assert_eq!(paper.kind, "news");
    assert_eq!(paper.tier(), Tier::Normal);
}

#[tokio::test]
async fn a_trust_list_beats_a_block_list_and_except_spares_a_host() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let sources = setting.service();
    let block = own(&sources, "block", &["good.org", "farm.org", "kept.org"]).await;
    own(&sources, "trust", &["good.org"]).await;
    let except = ListChange { except: Some(vec!["kept.org".to_owned()]), ..ListChange::default() };
    sources.change_list(&block.id, except).await.unwrap();

    assert_eq!(one(&sources, "good.org", WAIT).await.tier(), Tier::High);
    assert_eq!(one(&sources, "farm.org", WAIT).await.tier(), Tier::Avoid);
    assert_eq!(one(&sources, "sub.kept.org", WAIT).await.tier(), Tier::Unknown);
}

#[tokio::test]
async fn a_list_applies_once_on_and_stops_once_off_or_removed() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let sources = setting.service();
    let list = own(&sources, "block", &["farm.org"]).await;
    let off = ListChange { enabled: Some(false), ..ListChange::default() };

    sources.change_list(&list.id, off).await.unwrap();
    assert_eq!(one(&sources, "farm.org", WAIT).await.tier(), Tier::Unknown);

    let on = ListChange { enabled: Some(true), ..ListChange::default() };
    sources.change_list(&list.id, on).await.unwrap();
    assert_eq!(one(&sources, "farm.org", WAIT).await.tier(), Tier::Avoid);

    sources.remove_list(&list.id).await.unwrap();
    assert_eq!(one(&sources, "farm.org", WAIT).await.tier(), Tier::Unknown);
}

#[tokio::test]
async fn a_list_tag_shows_and_a_downloaded_list_fetches_on_its_own_terms() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let url = setting.list("/spam", body("||farm.org^\n")).await;
    let sources = setting.service();
    let change = ListChange {
        name: Some("Spam".to_owned()),
        url: Some(url),
        tag: Some("seo_spam".to_owned()),
        ..ListChange::default()
    };
    sources.add_list(change).await.unwrap();
    sources.refresh_lists().await;

    let found = one(&sources, "farm.org", WAIT).await;
    assert_eq!(found.tier(), Tier::Avoid);
    assert!(found.tags.contains("seo_spam"));
    assert_eq!(found.listed_by, ["Spam"]);
}

#[tokio::test]
async fn a_run_turns_lists_on_and_off_for_itself_alone() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let url = setting.list("/late", body("||late.org^\n")).await;
    let sources = setting.service();
    let on_for_all = own(&sources, "block", &["farm.org"]).await;
    let late = ListChange {
        name: Some("Late".to_owned()),
        url: Some(url),
        enabled: Some(false),
        ..ListChange::default()
    };
    let late = sources.add_list(late).await.unwrap().list;
    let policy = Policy { on: [late.id.clone()].into(), off: [on_for_all.id.clone()].into() };
    let hosts = ["farm.org".to_owned(), "late.org".to_owned()];

    sources.prepare(&policy).await;
    let run = sources.classify_with(&hosts, WAIT, &policy).await;
    assert_eq!(run["farm.org"].tier(), Tier::Unknown);
    assert_eq!(run["late.org"].tier(), Tier::Avoid);

    let everyone = sources.classify(&hosts, WAIT).await;
    assert_eq!(everyone["farm.org"].tier(), Tier::Avoid);
    assert_eq!(everyone["late.org"].tier(), Tier::Unknown);

    assert!(sources.check_policy(&policy).await.is_ok());
    let unknown = Policy { off: ["nope".to_owned()].into(), ..Policy::default() };
    assert!(matches!(sources.check_policy(&unknown).await, Err(ListError::Missing(_))));
}

#[tokio::test]
async fn a_list_reports_its_hosts_its_rules_and_its_last_failure() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let url = setting.list("/good", body("||farm.org^\n||farm.org/path^\n")).await;
    let gone = setting.list("/gone", ResponseTemplate::new(404)).await;
    let sources =
        setting.with(SourceSettings { lists: vec![format!("news={url}")], ..setting.settings() });
    let change = |url: &str| ListChange {
        name: Some("A list".to_owned()),
        url: Some(url.to_owned()),
        ..ListChange::default()
    };
    let good = sources.add_list(change(&url)).await.unwrap().list.id;
    let bad = sources.add_list(change(&gone)).await.unwrap().list.id;

    let fresh = sources.refresh_list(&good).await.unwrap();
    assert_eq!((fresh.hosts, fresh.skipped), (1, 1));
    assert!(fresh.fetched_at.is_some() && fresh.error.is_none());
    let failed = sources.refresh_list(&bad).await.unwrap();
    assert_eq!(failed.error.as_deref(), Some("status 404"));
    assert_eq!(failed.fetched_at, None);
    assert!(matches!(sources.refresh_list("config-0").await, Err(ListError::Missing(_))));

    let all = sources.list_statuses().await.unwrap();
    let config = all.last().unwrap();
    assert_eq!((config.list.group.as_str(), config.list.effect.as_str()), ("Config", "kind:news"));
    assert_eq!(config.hosts, 1);

    let explained = sources.explain("farm.org").await;
    assert_eq!(explained.source.listed_by, ["news", "A list"]);
    assert_eq!(explained.lists.len(), 2);
}

#[test]
fn a_list_counts_the_rules_it_cannot_use() {
    for (text, skipped) in
        [(HOSTS_FILE, 1), (UBLACKLIST, 2), (ADBLOCK, 3), ("# only\n! comments\n", 0)]
    {
        assert_eq!(parse_list(text).skipped, skipped, "{text}");
    }
}

#[test]
fn a_host_set_finds_exactly_its_hosts() {
    let set = lists::HostSet::new(["b.example", "a.example", "c.example"].map(str::to_owned));

    for host in ["a.example", "b.example", "c.example"] {
        assert!(set.contains(host), "{host}");
    }
    for host in ["", "a", "a.example.", "aa.example", "d.example", "example"] {
        assert!(!set.contains(host), "{host}");
    }
    assert!(!lists::HostSet::default().contains("a.example"));
}

#[test]
fn site_names_stop_at_the_registrable_domain() {
    assert_eq!(
        site_names("a.b.example.co.uk"),
        ["a.b.example.co.uk", "b.example.co.uk", "example.co.uk"]
    );
    assert_eq!(site_names("someone.github.io"), ["someone.github.io"]);
    assert_eq!(site_names("a.b.not-a-suffix"), ["a.b.not-a-suffix"]);
}

#[tokio::test]
async fn a_list_entry_covers_its_subdomains_but_not_those_of_a_public_suffix() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let url = setting.list("/farms.txt", body("blog.farm.org\ngithub.io\nco.uk\n")).await;
    let sources = setting
        .with(SourceSettings { lists: vec![format!("content_farm={url}")], ..setting.settings() });
    sources.refresh_lists().await;

    assert_eq!(one(&sources, "a.b.blog.farm.org", WAIT).await.tier(), Tier::Avoid);
    assert_eq!(one(&sources, "github.io", WAIT).await.tier(), Tier::Avoid);
    for host in ["farm.org", "someone.github.io", "bbc.co.uk"] {
        assert_ne!(one(&sources, host, WAIT).await.tier(), Tier::Avoid, "{host}");
    }
}

#[tokio::test]
async fn lists_naming_one_host_all_count() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let ai = setting.list("/ai.txt", body("both.gov\n")).await;
    let farm = setting.list("/farm.txt", body("both.gov\n")).await;
    let gov = setting.list("/gov.csv", body("both.gov\n")).await;
    let news = setting.list("/news.txt", body("both.gov\n")).await;
    let sources = setting.with(SourceSettings {
        lists: vec![
            format!("ai_generated={ai}"),
            format!("news={news}"),
            format!("government={gov}"),
            format!("content_farm={farm}"),
        ],
        ..setting.settings()
    });
    sources.refresh_lists().await;

    let found = one(&sources, "both.gov", WAIT).await;

    assert_eq!(found.kind, "government");
    assert_eq!(tags(&found), ["ai_generated", "content_farm"]);
    assert_eq!(found.origin, "list");
}

fn body(text: &str) -> ResponseTemplate {
    ResponseTemplate::new(200).set_body_string(text)
}

#[tokio::test]
async fn lists_are_downloaded_into_the_data_dir_and_label_their_hosts() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let ai = setting.list("/ai.txt", body(HOSTS_FILE)).await;
    let gov = setting.list("/gov.csv", body(CISA)).await;
    let sources = setting.with(SourceSettings {
        lists: vec![format!("ai_generated={ai}"), format!("government={gov}")],
        ..setting.settings()
    });

    sources.refresh_lists().await;
    let hosts = ["forkful.ai", "29palms.gov", "city.29palms.gov"].map(str::to_owned);
    let found = sources.classify(&hosts, WAIT).await;
    sources.refresh_lists().await;

    assert_eq!(found["forkful.ai"].tier(), Tier::Avoid);
    assert_eq!(found["29palms.gov"].origin, "list");
    assert_eq!(found["29palms.gov"].kind, "government");
    assert_eq!(found["city.29palms.gov"].kind, "government");
    let lists: usize = {
        let seen = setting.server.received_requests().await.unwrap();
        seen.iter().filter(|request| request.url.path() != "/sparql").count()
    };
    assert_eq!((lists, setting.list_files()), (2, 2));
}

#[tokio::test]
async fn a_list_file_keeps_the_name_python_gave_it() {
    let setting = Setting::new().await;
    let sources = setting.service();
    let name = sources.list_path("https://lists.example/ai.txt");
    assert_eq!(name.file_name().unwrap(), "sources-55d728f153c5.txt");
}

#[tokio::test]
async fn a_list_older_than_ttl_days_is_fetched_again_and_a_failure_keeps_the_copy() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    Mock::given(path("/ai.txt"))
        .respond_with(body(HOSTS_FILE))
        .up_to_n_times(1)
        .mount(&setting.server)
        .await;
    let url = setting.list("/ai.txt", ResponseTemplate::new(503)).await;
    let sources = setting
        .with(SourceSettings { lists: vec![format!("ai_generated={url}")], ..setting.settings() });
    sources.refresh_lists().await;
    let old = SystemTime::now() - Duration::from_secs(31 * 86400);
    let file = std::fs::File::options().write(true).open(sources.list_path(&url)).unwrap();
    file.set_modified(old).unwrap();

    sources.refresh_lists().await;

    let seen = setting.server.received_requests().await.unwrap();
    assert_eq!(seen.iter().filter(|request| request.url.path() == "/ai.txt").count(), 2);
    assert_eq!(one(&sources, "forkful.ai", Duration::ZERO).await.tier(), Tier::Avoid);
}

#[tokio::test]
async fn a_list_with_an_unknown_label_is_ignored() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let url = setting.list("/ai.txt", body(HOSTS_FILE)).await;
    let sources =
        setting.with(SourceSettings { lists: vec![format!("shiny={url}")], ..setting.settings() });

    sources.refresh_lists().await;

    assert_eq!(one(&sources, "forkful.ai", Duration::ZERO).await.kind, "unknown");
}

#[tokio::test]
async fn a_list_not_downloaded_yet_is_skipped() {
    let setting = Setting::new().await;
    let sources = setting.with(SourceSettings {
        lists: vec!["government=https://lists.example/gov.csv".to_owned()],
        rsp: true,
        ..setting.settings()
    });

    let lists = sources.read_lists();

    assert!(lists.listed.is_empty() && lists.rsp.is_empty());
}

#[tokio::test]
async fn a_list_over_the_size_cap_or_not_utf8_is_skipped_without_failing() {
    let setting = Setting::new().await;
    setting.wdqs(Wdqs::default()).await;
    let url = setting.list("/ai.txt", body(HOSTS_FILE)).await;
    let mut sources = setting
        .built(SourceSettings { lists: vec![format!("ai_generated={url}")], ..setting.settings() });
    sources.max_list_bytes = 10;
    let sources = Arc::new(sources);

    sources.refresh_lists().await;
    assert_eq!(setting.list_files(), 0);

    std::fs::write(sources.list_path(&url), b"\xff\xfe\x00bad").unwrap();
    sources.refresh_lists().await;

    assert_eq!(one(&sources, "forkful.ai", Duration::ZERO).await.kind, "unknown");
}

fn row(status: &str, name: &str, uses: &str) -> String {
    let cell =
        if uses.is_empty() { "| ".to_owned() } else { format!("| {{{{WP:RSPUSES|{uses}}}}}") };
    format!(
        "|- class=\"s-{status}\" id=\"{name}\"\n| [[{name}]]\n| {{{{WP:RSPSTATUS|{status}}}}}\n\
         | Summary of {name}.\n{cell}\n"
    )
}

// Modelled on the API's answer for the page's transcluded subpages, with rows cut to the columns
// that matter.
fn rsp() -> String {
    let rows = [
        row("gr", "Reuters", "reuters.com"),
        row("nc", "Wired", "wired.com"),
        row("gu", "Quora", "quora.com|www.quora.example"),
        row("d", "Daily Mail", "dailymail.co.uk|pressreader.com/uk/daily-mail"),
        row("b", "Spam", "spam.example"),
        row("gr", "Guardian", "theguardian.com"),
        row("nc", "Guardian blogs", "theguardian.com"),
        row("gr", "Peerage", ""),
        row("m", "Polygon", "polygon.com"),
        row("gu", "Wikipedia", "wikipedia.org"),
    ]
    .concat();
    serde_json::json!({"query": {"pages": [
        {"title": "Wikipedia:RSPUSES", "revisions": [{"slots": {"main": {"content": "x"}}}]},
        {"title": "Gone"},
        {"title": "Perennial/1", "revisions": [{"slots": {"main": {
            "content": format!("{{{{WP:RSPTableFull}}}}\n{{| class=\"wikitable\"\n{rows}|}}"),
        }}}]},
    ]}})
    .to_string()
}

#[test]
fn the_perennial_sources_rows_give_a_tag_by_status_and_skip_what_has_no_site() {
    let found = parse_rsp(&rsp()).unwrap();
    let want = [
        ("reuters.com", "rsp_reliable"),
        ("wired.com", "rsp_mixed"),
        ("quora.com", "rsp_unreliable"),
        ("quora.example", "rsp_unreliable"),
        ("dailymail.co.uk", "rsp_deprecated"),
        ("spam.example", "rsp_deprecated"),
        // Two rows, a status each: the less reliable holds.
        ("theguardian.com", "rsp_mixed"),
    ];
    assert_eq!(found.len(), want.len(), "{found:?}");
    for (host, tag) in want {
        assert_eq!(found[host], tag, "{host}");
    }
}

async fn with_rsp(setting: &Setting, answer: &str, settings: SourceSettings) -> Arc<Sources> {
    setting.wdqs(Wdqs::default()).await;
    setting.list("/rsp", body(answer)).await;
    let sources = setting.with(settings);
    sources.refresh_lists().await;
    sources
}

#[tokio::test]
async fn rsp_tags_give_the_tier_and_leave_the_kind_to_the_rest() {
    let setting = Setting::new().await;
    let settings = SourceSettings { rsp: true, ..setting.settings() };
    let sources = with_rsp(&setting, &rsp(), settings).await;
    let hosts = [
        "reuters.com",
        "www.dailymail.co.uk",
        "m.quora.com",
        "wired.com",
        "agency.gov",
        "a.example",
    ]
    .map(str::to_owned);

    let found = sources.classify(&hosts, Duration::ZERO).await;

    let seen = setting.server.received_requests().await.unwrap();
    let list = seen.iter().find(|request| request.url.path() == "/rsp").unwrap();
    assert_eq!(list.headers.get("user-agent").unwrap(), sources.web.user_agent().as_str());
    assert_eq!(tags(&found["reuters.com"]), ["rsp_reliable"]);
    assert_eq!(found["reuters.com"].tier(), Tier::High);
    assert_eq!(found["reuters.com"].kind, "unknown");
    assert_eq!(found["www.dailymail.co.uk"].tier(), Tier::Avoid);
    assert_eq!(found["m.quora.com"].tier(), Tier::Low);
    assert_eq!(found["wired.com"].tier(), Tier::Unknown);
    assert!(found["agency.gov"].tags.is_empty());
    assert!(found["a.example"].tags.is_empty());
}

#[tokio::test]
async fn an_rsp_tag_joins_the_kind_a_rule_gives() {
    let setting = Setting::new().await;
    let settings = SourceSettings { rsp: true, ..setting.settings() };
    let answer = rsp().replace("reuters.com", "agency.gov");
    let sources = with_rsp(&setting, &answer, settings).await;

    let found = one(&sources, "agency.gov", Duration::ZERO).await;

    assert_eq!((found.kind.as_str(), found.origin.as_str()), ("government", "rule"));
    assert_eq!(tags(&found), ["rsp_reliable"]);
}

#[tokio::test]
async fn a_mark_or_a_config_entry_is_not_changed_by_the_rsp_overlay() {
    let setting = Setting::new().await;
    let settings = SourceSettings {
        rsp: true,
        allow: vec!["dailymail.co.uk".to_owned()],
        ..setting.settings()
    };
    let sources = with_rsp(&setting, &rsp(), settings).await;
    sources.mark("quora.com", "news").await.unwrap();

    assert_eq!(tags(&one(&sources, "dailymail.co.uk", Duration::ZERO).await), ["allow"]);
    assert!(one(&sources, "quora.com", Duration::ZERO).await.tags.is_empty());
}

#[tokio::test]
async fn rsp_off_or_sources_off_fetches_and_tags_nothing() {
    let setting = Setting::new().await;
    let sources = with_rsp(&setting, &rsp(), setting.settings()).await;
    assert!(one(&sources, "dailymail.co.uk", Duration::ZERO).await.tags.is_empty());

    let off = setting.with(SourceSettings { enabled: false, rsp: true, ..setting.settings() });
    off.refresh_lists().await;
    assert!(one(&off, "dailymail.co.uk", Duration::ZERO).await.tags.is_empty());

    let seen = setting.server.received_requests().await.unwrap();
    assert!(seen.iter().all(|request| request.url.path() != "/rsp"));
}

#[tokio::test]
async fn an_unreadable_rsp_download_keeps_the_page_untagged() {
    let setting = Setting::new().await;
    let settings = SourceSettings { rsp: true, ..setting.settings() };
    let sources = with_rsp(&setting, "<html>", settings).await;

    assert!(one(&sources, "dailymail.co.uk", Duration::ZERO).await.tags.is_empty());
}

#[tokio::test]
async fn describe_gives_the_info_of_a_host_or_of_a_url_or_nothing_when_off() {
    let setting = Setting::new().await;
    let sources = setting.service();
    let urls = ["https://cdc.gov/a".to_owned(), "not a url".to_owned()];

    let found = describe_urls(Some(&sources), &urls).await;

    assert_eq!(found["https://cdc.gov/a"].tier, Tier::High);
    assert_eq!(found["https://cdc.gov/a"].origin, "rule");
    assert_eq!(found["not a url"].kind, "unknown");
    assert!(describe_urls(None, &urls).await.is_empty());
    assert!(describe(None, &["cdc.gov".to_owned()], WAIT).await.is_empty());
}

#[test]
fn a_host_chain_stops_at_two_labels() {
    assert_eq!(chain("a.b.example.org"), ["a.b.example.org", "b.example.org", "example.org"]);
    assert_eq!(chain("example.org"), ["example.org"]);
}

mod survey {
    use std::io::Write;

    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::*;
    use crate::sources::survey::LIKELY_FARM;

    fn stamps(days: usize, per_day: usize) -> Vec<String> {
        (0..days)
            .flat_map(|day| {
                std::iter::repeat_n(
                    format!("2026-{:02}-{:02}", day / 28 + 1, day % 28 + 1),
                    per_day,
                )
            })
            .collect()
    }

    #[test]
    fn rate_leaves_out_the_busiest_day() {
        let mut regenerated = stamps(1, 672);
        regenerated.extend(stamps(5, 1));
        assert!(rate(&regenerated).unwrap() < 2.0);
        assert_eq!(rate(&stamps(10, 20)), Some(20.0));
    }

    #[test]
    fn rate_needs_enough_dates_and_more_than_one_day() {
        assert_eq!(rate(&stamps(9, 11)), None);
        assert_eq!(rate(&stamps(1, 500)), None);
    }

    #[test]
    fn only_dense_and_unranked_is_a_verdict() {
        let dense = Survey { urls: 500, dated: 500, rate: Some(5.0) };
        let slow = Survey { urls: 500, dated: 500, rate: Some(4.9) };
        let undated = Survey { urls: 90_000, dated: 0, rate: None };
        assert!(verdict(&dense, false).unwrap().contains("5.0 URLs a day over 500 dated"));
        assert_eq!(verdict(&dense, true), None);
        assert_eq!(verdict(&slow, false), None);
        assert_eq!(verdict(&undated, false), None);
    }

    fn tranco(lines: &str) -> Vec<u8> {
        let mut zip = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
        zip.start_file("top-1m.csv", zip::write::SimpleFileOptions::default()).unwrap();
        zip.write_all(lines.as_bytes()).unwrap();
        zip.finish().unwrap().into_inner()
    }

    #[test]
    fn tranco_is_read_from_its_zip() {
        let zip = tranco("1,google.com\n2,www.example.org\n3,bad name\n");
        let hosts = parse_tranco(&zip, 3).unwrap();
        assert_eq!(hosts.len(), 2);
        assert!(hosts.contains("google.com") && hosts.contains("example.org"));
        assert!(parse_tranco(b"not a zip", 1).is_err());
    }

    #[test]
    fn the_tranco_list_is_read_down_to_the_rank_asked() {
        let zip = tranco("1,first.org\n2,second.org\n3,third.org\n");
        let hosts = parse_tranco(&zip, 2).unwrap();
        assert!(hosts.contains("second.org") && !hosts.contains("third.org"));
    }

    /// A list of `effect` on `entries`, with the Tranco copy read once it applies.
    async fn guarded(site: &Site, effect: &str, entries: &[&str], guard: bool) {
        let change = ListChange {
            name: Some("Guarded".to_owned()),
            entries: Some(entries.iter().map(|entry| (*entry).to_owned()).collect()),
            effect: Some(effect.to_owned()),
            guard: Some(guard),
            ..ListChange::default()
        };
        site.sources.add_list(change).await.unwrap();
    }

    #[tokio::test]
    async fn the_guard_spares_ranked_and_suffix_sites_from_what_lowers_them() {
        let site = site("1,popular.org\n2,farm.org\n", |config| {
            config.survey = false;
            config.guard_rank = 1;
        })
        .await;
        guarded(
            &site,
            "block",
            &["popular.org", "news.popular.org", "farm.org", "agency.gov"],
            true,
        )
        .await;

        for host in ["popular.org", "news.popular.org"] {
            assert_eq!(classify(&site, host).await.tier(), Tier::Unknown, "{host}");
        }
        assert_eq!(classify(&site, "agency.gov").await.kind, "government");
        assert_eq!(classify(&site, "farm.org").await.tier(), Tier::Avoid);
    }

    #[tokio::test]
    async fn a_list_with_its_guard_off_or_a_rank_of_zero_spares_only_as_asked() {
        let off = site("1,popular.org\n", |config| config.survey = false).await;
        guarded(&off, "block", &["popular.org"], false).await;
        assert_eq!(classify(&off, "popular.org").await.tier(), Tier::Avoid);

        let zero = site("1,popular.org\n", |config| {
            config.survey = false;
            config.guard_rank = 0;
        })
        .await;
        guarded(&zero, "block", &["popular.org", "agency.gov"], true).await;
        assert_eq!(classify(&zero, "popular.org").await.tier(), Tier::Avoid);
        assert_eq!(classify(&zero, "agency.gov").await.kind, "government");
    }

    #[tokio::test]
    async fn the_guard_leaves_kinds_alone_and_downloads_tranco_only_when_needed() {
        let site = site("1,popular.org\n", |config| config.survey = false).await;
        assert!(site.sources.fetches().is_empty());

        guarded(&site, "kind:news", &["popular.org"], true).await;
        assert_eq!(classify(&site, "popular.org").await.kind, "news");
        assert!(site.sources.fetches().is_empty());

        guarded(&site, "demote", &["other.org"], true).await;
        assert!(site.sources.fetches().iter().any(|fetch| fetch.name == "tranco"));
    }

    fn sitemap(days: usize, per_day: usize) -> String {
        let urls: String = stamps(days, per_day)
            .iter()
            .enumerate()
            .map(|(at, stamp)| format!("<url><loc>x/{at}</loc><lastmod>{stamp}</lastmod></url>"))
            .collect();
        format!("<urlset>{urls}</urlset>")
    }

    struct Site {
        server: MockServer,
        sources: Arc<Sources>,
        _dir: TempDir,
    }

    /// A service surveying a fake site, with `popular` in its Tranco list and Wikidata off.
    async fn site(popular: &str, settings: impl FnOnce(&mut SourceSettings)) -> Site {
        let server = MockServer::start().await;
        let dir = TempDir::new().unwrap();
        let key = kvasir_db::Key::for_tests();
        let store = SourceStore::open(&dir.path().join("sources.db"), &key).unwrap();
        super::turn_off_shipped_list(dir.path());
        let mut config = SourceSettings {
            enabled: true,
            wikidata: false,
            survey: true,
            lists: Vec::new(),
            ..SourceSettings::default()
        };
        settings(&mut config);
        let options = Options {
            contact: "me@example.org".to_owned(),
            host_interval: Duration::ZERO,
            render: false,
            stealth: false,
            max_age_hours: 48,
        };
        let web = Arc::new(Web::new(options, 8, Guard::Loopback, system_lookup()));
        let mut sources = Sources::new(config, store, dir.path().to_owned(), web);
        sources.tranco_url = format!("{}/tranco.zip", server.uri());
        sources.survey_base = Some(server.uri());
        let sources = Arc::new(sources);
        let path = sources.list_path(&sources.tranco_url);
        if !popular.is_empty() {
            std::fs::write(path, tranco(popular)).unwrap();
        }
        sources.load_lists().await;
        Site { server, sources, _dir: dir }
    }

    async fn serve(site: &Site, at: &str, body: String) {
        Mock::given(method("GET"))
            .and(path(at.to_owned()))
            .respond_with(ResponseTemplate::new(200).set_body_string(body))
            .mount(&site.server)
            .await;
    }

    async fn classify(site: &Site, host: &str) -> Source {
        let hosts = [host.to_owned()];
        site.sources.classify(&hosts, Duration::ZERO).await.remove(host).unwrap()
    }

    /// Classifies `host`, lets its survey finish, and classifies it again.
    async fn surveyed(site: &Site, host: &str) -> Source {
        classify(site, host).await;
        for _ in 0..500 {
            tokio::time::sleep(Duration::from_millis(10)).await;
            if !site.sources.surveying.load(std::sync::atomic::Ordering::SeqCst) {
                break;
            }
        }
        classify(site, host).await
    }

    fn hits(requests: &[Request], at: &str) -> usize {
        requests.iter().filter(|request| request.url.path() == at).count()
    }

    #[tokio::test]
    async fn a_dense_unranked_site_is_tagged_low_with_its_evidence() {
        let site = site("1,google.com\n", |_| {}).await;
        serve(&site, "/sitemap.xml", sitemap(10, 20)).await;

        let source = surveyed(&site, "blog.farm.org").await;

        assert!(source.tags.contains(LIKELY_FARM));
        assert_eq!(source.tier(), Tier::Low);
        assert_eq!(source.origin, "survey");
        assert!(source.evidence.contains("20.0 URLs a day"), "{}", source.evidence);
        // A site's other hosts share its verdict, which outlives a restart.
        site.sources.flags.lock().unwrap().clone_from(&Arc::default());
        site.sources.load_flags().await;
        assert!(classify(&site, "farm.org").await.tags.contains(LIKELY_FARM));
    }

    #[tokio::test]
    async fn a_ranked_site_and_a_slow_site_stay_unknown() {
        let site = site("1,popular.example\n", |_| {}).await;
        serve(&site, "/sitemap.xml", sitemap(10, 20)).await;
        assert_eq!(surveyed(&site, "popular.example").await.tier(), Tier::Unknown);

        let slow = self::site("1,google.com\n", |_| {}).await;
        serve(&slow, "/sitemap.xml", sitemap(150, 1)).await;
        assert_eq!(surveyed(&slow, "slow.example").await.tier(), Tier::Unknown);
    }

    #[tokio::test]
    async fn without_the_tranco_list_nothing_is_read() {
        let site = site("", |_| {}).await;
        serve(&site, "/sitemap.xml", sitemap(10, 20)).await;

        assert_eq!(surveyed(&site, "farm.example").await.tier(), Tier::Unknown);

        let requests = site.server.received_requests().await.unwrap();
        assert_eq!(hits(&requests, "/sitemap.xml"), 0);
    }

    #[tokio::test]
    async fn an_avoid_site_or_one_with_a_kind_is_never_read() {
        let site = site("1,google.com\n", |settings| {
            settings.deny = vec!["denied.example".to_owned()];
        })
        .await;
        serve(&site, "/sitemap.xml", sitemap(10, 20)).await;
        site.sources.mark("marked.example", "deny").await.unwrap();

        assert_eq!(surveyed(&site, "denied.example").await.tier(), Tier::Avoid);
        assert_eq!(surveyed(&site, "marked.example").await.tier(), Tier::Avoid);
        assert_eq!(surveyed(&site, "deepwiki.com").await.tier(), Tier::Avoid);
        assert_eq!(surveyed(&site, "whitehouse.gov").await.tier(), Tier::High);

        assert!(site.server.received_requests().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn a_mark_set_after_the_survey_beats_it() {
        let site = site("1,google.com\n", |_| {}).await;
        serve(&site, "/sitemap.xml", sitemap(10, 20)).await;
        assert_eq!(surveyed(&site, "farm.example").await.tier(), Tier::Low);

        site.sources.mark("farm.example", "allow").await.unwrap();

        assert_eq!(classify(&site, "farm.example").await.tier(), Tier::High);
    }

    #[tokio::test]
    async fn clearing_the_cache_forgets_a_survey() {
        let site = site("1,google.com\n", |_| {}).await;
        serve(&site, "/sitemap.xml", sitemap(10, 20)).await;
        assert_eq!(surveyed(&site, "farm.example").await.tier(), Tier::Low);

        site.sources.flush().await.unwrap();

        assert!(site.sources.flag(Source::new("farm.example", "unknown", &[], "")).tags.is_empty());
    }

    #[tokio::test]
    async fn the_sitemaps_robots_names_are_read_through_an_index() {
        let site = site("1,google.com\n", |_| {}).await;
        let uri = site.server.uri();
        serve(&site, "/robots.txt", format!("User-agent: *\nSitemap: {uri}/index.xml\n")).await;
        let index = format!(
            "<sitemapindex><sitemap><loc>{uri}/a.xml</loc></sitemap>\
             <sitemap><loc>https://elsewhere.example/b.xml</loc></sitemap></sitemapindex>"
        );
        serve(&site, "/index.xml", index).await;
        serve(&site, "/a.xml", sitemap(10, 20)).await;

        assert_eq!(surveyed(&site, "farm.example").await.tier(), Tier::Low);
    }

    #[tokio::test]
    async fn a_site_past_its_ttl_is_surveyed_again_and_can_lose_the_tag() {
        let site = site("1,google.com\n", |settings| settings.ttl_days = 0).await;
        serve(&site, "/sitemap.xml", sitemap(10, 20)).await;
        assert_eq!(surveyed(&site, "farm.example").await.tier(), Tier::Low);

        site.server.reset().await;
        serve(&site, "/sitemap.xml", sitemap(150, 1)).await;

        // The first survey since the reset runs as this classification starts it.
        assert_eq!(surveyed(&site, "farm.example").await.tier(), Tier::Unknown);
    }

    #[tokio::test]
    async fn the_tranco_list_is_downloaded_only_with_the_survey() {
        let on = site("", |_| {}).await;
        assert!(on.sources.fetches().iter().any(|fetch| fetch.name == "tranco"));
        let off = site("", |settings| settings.survey = false).await;
        assert!(off.sources.fetches().is_empty());
    }
}
