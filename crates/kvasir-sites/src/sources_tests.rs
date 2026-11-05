//! The order a host resolves in, and what is asked of Wikidata and kept.

use kvasir_db::rusqlite::Connection;
use kvasir_fetch::{Guard, Options, system_lookup};
use tempfile::TempDir;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

use super::*;

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

struct Setting {
    server: MockServer,
    store: SourceStore,
    dir: TempDir,
}

impl Setting {
    async fn new() -> Self {
        let dir = TempDir::new().unwrap();
        let store = SourceStore::open(&dir.path().join("sources.db")).unwrap();
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
        Connection::open(self.dir.path().join("sources.db"))
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
const ADBLOCK: &str = "! Title: x\ndomain.example##a[href*=\"y\"]\n";

#[test]
fn lists_are_read_as_hosts_ublacklist_or_csv() {
    for (text, want) in [
        (HOSTS_FILE, &["cgdream.ai", "forkful.ai", "tryleap.ai"][..]),
        (UBLACKLIST, &["krisp.ai", "otter.ai"]),
        (CISA, &["29palms.gov", "abbeville.gov"]),
        (ADBLOCK, &[]),
    ] {
        let mut got: Vec<String> = parse_list(text).into_iter().collect();
        got.sort();
        assert_eq!(got, want);
    }
}

#[test]
fn a_list_may_end_its_lines_in_a_lone_carriage_return() {
    let got = parse_list("a.example\rb.example\r");
    assert_eq!(got, HashSet::from(["a.example".to_owned(), "b.example".to_owned()]));
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

    assert!(lists.index.is_empty() && lists.rsp.is_empty());
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
