//! Reading a page from the web: who may be asked and how often, what is kept, and what a page that
//! a plain request cannot read falls back to.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use arc_swap::ArcSwap;
use kvasir_browser::{BoxFuture, Render, RenderError};
use kvasir_extract::{Link, MAX_CHARACTERS, Reason, Static, Unusable, check_page, head, read_html};
use reqwest::header::{self, HeaderMap, HeaderValue};
use reqwest::redirect::{Attempt, Policy};
use reqwest::{Client, Response, StatusCode};
use tokio::time::Instant;
use url::{Host, Url};

mod guard;
mod polite;
mod robots;
mod store;

pub use guard::{Guard, GuardedResolver, Lookup, is_public, system_lookup};
pub use polite::{HOST_CONCURRENCY, Politeness, Slot};
pub use robots::Robots;
pub use store::{Cached, Found, PageStore, Row, Stats, domain, page_id};

/// A read that stalls this long has failed.
pub const TIMEOUT: Duration = Duration::from_secs(20);
// A site that keeps sending just enough to pass the above cannot hold a request for longer.
const TOTAL_TIMEOUT: Duration = Duration::from_secs(120);
/// A download stops here. A PDF cut short does not parse, and falls back to its snippet like any
/// other unreadable page.
pub const MAX_BYTES: usize = 20_000_000;
// As httpx does.
const MAX_REDIRECTS: usize = 20;
// A Retry-After longer than this is not waited out: a run would sit on one page. Wikimedia's policy
// asks for a pause of this length when a 429 names none.
const MAX_RETRY_AFTER: f64 = 30.0;
const DEFAULT_RETRY_AFTER: f64 = 5.0;
// RFC 9309 asks for robots.txt to be read again after 24 hours, and for five redirects.
const ROBOTS_MAX_AGE: Duration = Duration::from_secs(24 * 3600);
const ROBOTS_MAX_REDIRECTS: usize = 5;
const SECONDS_PER_HOUR: f64 = 3600.0;
const SITE: &str = "https://github.com/brewcoua/kvasir";

/// Unix seconds, now.
pub fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

/// How a page was read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Engine {
    Static,
    Render,
    Stealth,
}

impl Engine {
    pub fn name(self) -> &'static str {
        match self {
            Self::Static => "static",
            Self::Render => "render",
            Self::Stealth => "stealth",
        }
    }

    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "static" => Some(Self::Static),
            "render" => Some(Self::Render),
            "stealth" => Some(Self::Stealth),
            _ => None,
        }
    }
}

/// A page read: its text, and the links found before any of the page was dropped.
#[derive(Clone, Debug, PartialEq)]
pub struct Page {
    pub url: String,
    pub final_url: String,
    pub title: String,
    pub text: String,
    pub engine: Engine,
    pub links: Vec<Link>,
    /// Unix seconds when the page was read from its site, which a cached page keeps.
    pub fetched_at: f64,
}

/// The page could not be read as text.
#[derive(Clone, Debug, thiserror::Error)]
pub enum FetchError {
    #[error("{0}")]
    Failed(String),
    /// A static page that holds no article.
    #[error(transparent)]
    Unusable(#[from] Unusable),
}

/// What can change while runs fetch: what each host was last asked, and any wait it asked for, is
/// kept.
#[derive(Clone, Debug, PartialEq)]
pub struct Options {
    /// Named in the user agent, or empty.
    pub contact: String,
    pub host_interval: Duration,
    /// Whether a page that needs JavaScript is rendered in a browser.
    pub render: bool,
    /// Whether rendering goes through the stealth browser.
    pub stealth: bool,
    /// Pages under this age are served from the cache.
    pub max_age_hours: u32,
}

/// How the service reaches the web.
pub struct Web {
    guard: Guard,
    lookup: Lookup,
    // Follows redirects, checking each hop.
    pages: Client,
    // Follows none, for whoever walks them by hand.
    single: Client,
    // Follows none and checks nothing.
    unguarded: Client,
    polite: Politeness,
    options: ArcSwap<Options>,
    cache: ArcSwap<Option<PageStore>>,
    renderer: ArcSwap<Option<Arc<dyn Render>>>,
    pdf_reader: (PathBuf, Vec<String>),
    robots: Mutex<HashMap<String, (Option<Robots>, Instant)>>,
}

fn redirect(attempt: Attempt, guard: Guard) -> reqwest::redirect::Action {
    if attempt.previous().len() >= MAX_REDIRECTS {
        attempt.error("too many redirects")
    } else if !guard.allows_url(attempt.url()) {
        let refused = format!("redirect to {} refused", attempt.url());
        attempt.error(refused)
    } else {
        attempt.follow()
    }
}

fn client(policy: Policy, resolver: Option<GuardedResolver>) -> Client {
    // A proxy would resolve names itself, out of reach of the resolver.
    let client = Client::builder()
        .redirect(policy)
        .connect_timeout(TIMEOUT)
        .read_timeout(TIMEOUT)
        .timeout(TOTAL_TIMEOUT)
        .no_proxy();
    let client = match resolver {
        Some(resolver) => client.dns_resolver(Arc::new(resolver)),
        None => client,
    };
    client.build().expect("the TLS backend starts")
}

impl Web {
    /// `permits` caps the requests the process has open.
    pub fn new(options: Options, permits: usize, guard: Guard, lookup: Lookup) -> Self {
        let resolver = || Some(GuardedResolver(Arc::clone(&lookup), guard));
        let pdf_reader = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("kvasir"));
        Self {
            guard,
            lookup: Arc::clone(&lookup),
            pages: client(Policy::custom(move |attempt| redirect(attempt, guard)), resolver()),
            single: client(Policy::none(), resolver()),
            unguarded: client(Policy::none(), None),
            polite: Politeness::new(permits),
            options: ArcSwap::from_pointee(options),
            cache: ArcSwap::from_pointee(None),
            renderer: ArcSwap::from_pointee(None),
            pdf_reader: (pdf_reader, vec!["pdf-text".to_owned()]),
            robots: Mutex::new(HashMap::new()),
        }
    }

    /// Reads PDFs with `program args` rather than this binary's `pdf-text`.
    pub fn with_pdf_reader(mut self, program: PathBuf, args: Vec<String>) -> Self {
        self.pdf_reader = (program, args);
        self
    }

    pub fn configure(&self, options: Options) {
        self.options.store(Arc::new(options));
    }

    pub fn options(&self) -> Arc<Options> {
        self.options.load_full()
    }

    /// Serves pages from `store` and keeps what is read. None caches nothing.
    pub fn set_cache(&self, store: Option<PageStore>) {
        self.cache.store(Arc::new(store));
    }

    pub fn cache(&self) -> Option<PageStore> {
        (**self.cache.load()).clone()
    }

    pub fn set_renderer(&self, renderer: Option<Arc<dyn Render>>) {
        self.renderer.store(Arc::new(renderer));
    }

    /// The client that follows no redirect, for those who check each hop themselves.
    pub fn single(&self) -> &Client {
        &self.single
    }

    /// A client that follows no redirect and checks no address, for what the operator configured,
    /// which may be on their own network. Never for a URL that came from a page.
    pub fn unguarded(&self) -> &Client {
        &self.unguarded
    }

    /// Which addresses this process may reach.
    pub fn guard(&self) -> Guard {
        self.guard
    }

    /// The check a browser makes of each request a page sends. A browser does its own resolving, so
    /// the name is resolved here, before it does.
    pub fn page_check(&self) -> Arc<dyn Fn(String) -> BoxFuture<'static, bool> + Send + Sync> {
        let (guard, lookup) = (self.guard, Arc::clone(&self.lookup));
        Arc::new(move |url| {
            let lookup = Arc::clone(&lookup);
            Box::pin(async move {
                let Some(parsed) = Url::parse(&url).ok().filter(|url| guard.allows_url(url)) else {
                    return false;
                };
                let Some(Host::Domain(name)) = parsed.host() else {
                    return true;
                };
                lookup(name.to_owned()).await.is_ok_and(|found| {
                    !found.is_empty() && found.into_iter().all(|ip| guard.allows(ip))
                })
            })
        })
    }

    /// Holds a request's place with `host` and the process.
    pub async fn slot(&self, host: &str) -> Slot {
        self.polite.slot(host, self.options().host_interval).await
    }

    /// Holds one of the process's permits, and no place with a host.
    pub async fn permit(&self) -> tokio::sync::OwnedSemaphorePermit {
        self.polite.permit().await
    }

    /// Names the service, where to read about it and who runs it, as Wikimedia's policy asks.
    pub fn user_agent(&self) -> String {
        let options = self.options();
        let version = env!("CARGO_PKG_VERSION");
        if options.contact.is_empty() {
            format!("kvasir/{version} (+{SITE}) reqwest/0.13")
        } else {
            format!("kvasir/{version} (+{SITE}; {}) reqwest/0.13", options.contact)
        }
    }

    /// Whether every address `host` resolves to is on the public internet.
    pub async fn public(&self, host: &str) -> bool {
        let bare = host.trim_start_matches('[').trim_end_matches(']');
        if let Ok(address) = bare.parse() {
            return self.guard.allows(address);
        }
        match (self.lookup)(host.to_owned()).await {
            Ok(found) => !found.is_empty() && found.into_iter().all(|ip| self.guard.allows(ip)),
            Err(_) => false,
        }
    }

    /// Whether `host` resolves at all, private or not.
    pub async fn resolves(&self, host: &str) -> bool {
        (self.lookup)(host.to_owned()).await.is_ok_and(|found| !found.is_empty())
    }

    /// Whether a lead may be read: its host is public, and the robots.txt of its site lets this
    /// crawler read it. No robots.txt, or a 4xx, allows all (RFC 9309). A 5xx or no answer allows
    /// none: a lead costs little to skip.
    pub async fn allowed(&self, url: &str) -> bool {
        let Ok(parsed) = Url::parse(url) else {
            return false;
        };
        let host = parsed.host_str().unwrap_or("");
        // A lead's URL is written by the page it was on, so it must not reach this service's
        // network.
        if !self.public(host).await {
            return false;
        }
        let origin = parsed.origin().ascii_serialization();
        let cached = {
            let robots = self.robots.lock().unwrap_or_else(|e| e.into_inner());
            robots.get(&origin).filter(|(_, read)| read.elapsed() < ROBOTS_MAX_AGE).cloned()
        };
        let rules = match cached {
            Some((rules, _)) => rules,
            None => {
                let rules = self.read_robots(&origin, host).await;
                let mut robots = self.robots.lock().unwrap_or_else(|e| e.into_inner());
                robots.retain(|_, (_, read)| read.elapsed() < ROBOTS_MAX_AGE);
                robots.insert(origin, (rules.clone(), Instant::now()));
                rules
            }
        };
        let path = match parsed.query() {
            Some(query) => format!("{}?{query}", parsed.path()),
            None => parsed.path().to_owned(),
        };
        rules.is_some_and(|rules| rules.allows(&path))
    }

    /// The rules of `origin`. None allows nothing; rules that are empty allow all.
    async fn read_robots(&self, origin: &str, host: &str) -> Option<Robots> {
        let _slot = self.slot(host).await;
        let mut url = Url::parse(&format!("{origin}/robots.txt")).ok()?;
        // By hand, because a redirect must not reach this service's network either.
        let mut response = None;
        for _ in 0..=ROBOTS_MAX_REDIRECTS {
            let got = self
                .single
                .get(url.clone())
                .header(header::USER_AGENT, self.user_agent())
                .send()
                .await
                .ok()?;
            if !got.status().is_redirection() {
                response = Some(got);
                break;
            }
            let location = got.headers().get(header::LOCATION)?.to_str().ok()?;
            url = url.join(location).ok()?;
            if !self.guard.allows_url(&url) {
                return None;
            }
        }
        let response = response?;
        let status = response.status();
        if status.is_client_error() {
            return Some(Robots::default());
        }
        if !status.is_success() {
            return None;
        }
        let body = read_capped(response).await.ok()?;
        Some(Robots::parse(head(&String::from_utf8_lossy(&body), MAX_CHARACTERS)))
    }

    /// An HTML page or a PDF, as text and links. Other formats are refused, not parsed.
    ///
    /// With a cache set, a fresh copy is returned without a request, a stale one is revalidated,
    /// and a page read from its site replaces the copy. A failure leaves the copy alone. `max_age`
    /// overrides the configured age for this read, and 0 means read again whatever the copy says.
    pub async fn fetch_page(&self, url: &str, max_age: Option<u32>) -> Result<Page, FetchError> {
        let Some(store) = self.cache() else {
            let (page, _, _) = self.download(url, HeaderMap::new()).await?.expect("no validators");
            return Ok(page);
        };
        let max_age = max_age.unwrap_or(self.options().max_age_hours);
        // The cache saves downloads; a cache that fails, a disk full or a lock held too long, costs
        // them but must not cost the run.
        let cached = if max_age == 0 {
            None
        } else {
            store.get(url).await.unwrap_or_else(|e| {
                tracing::warn!("page cache unreadable, fetching {url}: {e}");
                None
            })
        };
        let mut validators = HeaderMap::new();
        if let Some(cached) = &cached {
            if now() - cached.page.fetched_at < f64::from(max_age) * SECONDS_PER_HOUR {
                return Ok(cached.page.clone());
            }
            let value = |text: &Option<String>| {
                text.as_deref().and_then(|text| HeaderValue::from_str(text).ok())
            };
            if let Some(etag) = value(&cached.etag) {
                validators.insert(header::IF_NONE_MATCH, etag);
            }
            if let Some(modified) = value(&cached.last_modified) {
                validators.insert(header::IF_MODIFIED_SINCE, modified);
            }
        }
        let Some((page, etag, modified)) = self.download(url, validators).await? else {
            let mut page = cached.expect("validators came from a copy").page;
            match store.touch(url).await {
                Ok(at) => page.fetched_at = at,
                Err(e) => tracing::warn!("page cache not refreshed for {url}: {e}"),
            }
            return Ok(page);
        };
        if let Err(e) = store.put(&page, etag.as_deref(), modified.as_deref()).await {
            tracing::warn!("page cache not written for {url}: {e}");
        }
        Ok(page)
    }

    /// The page, with the ETag and Last-Modified its site sent, or None when `validators` were sent
    /// and the site answered 304.
    async fn download(
        &self,
        url: &str,
        validators: HeaderMap,
    ) -> Result<Option<Downloaded>, FetchError> {
        let failed = |reason: String| FetchError::Failed(format!("{url}: {reason}"));
        let parsed = Url::parse(url).map_err(|e| failed(e.to_string()))?;
        if !self.guard.allows_url(&parsed) {
            return Err(failed("not on the public internet".to_owned()));
        }
        // The host asked is the one in the URL; a redirect to another is not counted against it.
        let host = parsed.host_str().unwrap_or("").to_owned();
        let has_validators = !validators.is_empty();
        let render_on = self.options().render;
        let mut retried = false;
        let (final_url, content_type, etag, modified, charset, body) = loop {
            let slot = self.slot(&host).await;
            let response = self
                .pages
                .get(parsed.clone())
                .header(header::USER_AGENT, self.user_agent())
                .headers(validators.clone())
                .send()
                .await
                .map_err(|e| failed(describe(e)))?;
            let status = response.status();
            if status == StatusCode::TOO_MANY_REQUESTS {
                let asked = header_text(&response, header::RETRY_AFTER);
                let delay = retry_after(asked.as_deref());
                if retried || delay > MAX_RETRY_AFTER {
                    return Err(failed(format!("rate limited, retry after {delay}s")));
                }
                // The retry waits for its slot, after this one is released.
                self.polite.delay(&host, Duration::from_secs_f64(delay));
                retried = true;
                drop(slot);
                continue;
            }
            if status == StatusCode::NOT_MODIFIED && has_validators {
                return Ok(None);
            }
            // What a challenge or a block answers with, before any page of its own: worth a
            // browser's try.
            if render_on && matches!(status.as_u16(), 403 | 503) {
                // Rendered once this slot is released: the render takes a permit of its own, and
                // holding this one meanwhile can exhaust them.
                drop(slot);
                let blocked = Unusable {
                    message: format!("{url}: {}", status.as_u16()),
                    reason: Reason::Blocked,
                };
                return self.rendered(url, blocked).await.map(|page| Some((page, None, None)));
            }
            if !status.is_success() {
                return Err(failed(format!("{}", status.as_u16())));
            }
            let content_type =
                header_text(&response, header::CONTENT_TYPE).unwrap_or_default().to_lowercase();
            // Many servers send a PDF as a generic download, so those are read and sniffed.
            let readable = ["html", "pdf", "octet-stream"];
            if !content_type.is_empty() && !readable.iter().any(|kind| content_type.contains(kind))
            {
                return Err(failed(format!("not HTML or PDF ({content_type})")));
            }
            let final_url = response.url().to_string();
            let etag = header_text(&response, header::ETAG);
            let modified = header_text(&response, header::LAST_MODIFIED);
            let body = read_capped(response).await.map_err(|e| failed(describe(e)))?;
            let charset = charset_of(&content_type);
            break (final_url, content_type, etag, modified, charset, body);
        };
        let page = if content_type.contains("html") {
            let encoding = charset
                .and_then(|label| encoding_rs::Encoding::for_label(label.as_bytes()))
                .unwrap_or(encoding_rs::UTF_8);
            let html = encoding.decode(&body).0.into_owned();
            let html = head(&html, MAX_CHARACTERS).to_owned();
            let (url_owned, final_owned) = (url.to_owned(), final_url.clone());
            let read = tokio::task::spawn_blocking(move || {
                check_page(&url_owned, &html)?;
                Ok::<Static, Unusable>(read_html(&html, &final_owned))
            })
            .await
            .map_err(|_| failed("cannot be read".to_owned()))?;
            match read {
                Ok(read) => page_of(url, final_url, read, Engine::Static),
                Err(unusable) => {
                    if !render_on {
                        return Err(unusable.into());
                    }
                    return self.rendered(url, unusable).await.map(|page| Some((page, None, None)));
                }
            }
        } else if body.starts_with(b"%PDF-") {
            let (program, args) = &self.pdf_reader;
            let args: Vec<&str> = args.iter().map(String::as_str).collect();
            let text =
                kvasir_extract::pdf::read(program, &args, body, kvasir_extract::pdf::TIMEOUT)
                    .await
                    .map_err(failed)?;
            Page {
                url: url.to_owned(),
                final_url,
                title: String::new(),
                text,
                engine: Engine::Static,
                links: Vec::new(),
                fetched_at: now(),
            }
        } else {
            let named = if content_type.is_empty() { "no content type" } else { &content_type };
            return Err(failed(format!("not HTML or PDF ({named})")));
        };
        Ok(Some((page, etag, modified)))
    }

    /// The page as a browser builds it, for one whose static HTML was a shell, a challenge or a
    /// block. Without a browser to build it, the static page's failure stands.
    async fn rendered(&self, url: &str, statically: Unusable) -> Result<Page, FetchError> {
        let Some(renderer) = (**self.renderer.load()).clone() else {
            return Err(statically.into());
        };
        let failed = |reason: String| FetchError::Failed(reason);
        let host = Url::parse(url).ok().and_then(|url| url.host_str().map(str::to_owned));
        let rendered = {
            let _slot = self.slot(host.as_deref().unwrap_or("")).await;
            renderer.render(url).await
        };
        let rendered = match rendered {
            Ok(rendered) => rendered,
            Err(RenderError::Unavailable(_)) => return Err(statically.into()),
            Err(RenderError::Failed(reason)) => return Err(failed(reason)),
        };
        if rendered.status >= 400 {
            return Err(failed(format!("{url}: {} when rendered", rendered.status)));
        }
        let engine = if self.options().stealth { Engine::Stealth } else { Engine::Render };
        let url_owned = url.to_owned();
        let final_url = rendered.final_url.clone();
        let read = tokio::task::spawn_blocking(move || {
            check_page(&url_owned, &rendered.html)?;
            let html = head(&rendered.html, MAX_CHARACTERS);
            Ok::<Static, Unusable>(read_html(html, &rendered.final_url))
        })
        .await
        .map_err(|_| failed(format!("{url}: cannot be read")))??;
        Ok(page_of(url, final_url, read, engine))
    }
}

type Downloaded = (Page, Option<String>, Option<String>);

fn page_of(url: &str, final_url: String, read: Static, engine: Engine) -> Page {
    Page {
        url: url.to_owned(),
        final_url,
        title: read.title,
        text: read.text,
        engine,
        links: read.links,
        fetched_at: now(),
    }
}

/// What failed, without the URL the caller already names.
fn describe(error: reqwest::Error) -> String {
    use std::error::Error;
    let error = error.without_url();
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(inner) = source {
        text.push_str(": ");
        text.push_str(&inner.to_string());
        source = inner.source();
    }
    text
}

fn header_text(response: &Response, name: header::HeaderName) -> Option<String> {
    response.headers().get(name)?.to_str().ok().map(str::to_owned)
}

fn charset_of(content_type: &str) -> Option<String> {
    content_type.split(';').skip(1).find_map(|part| {
        let (key, value) = part.split_once('=')?;
        (key.trim().eq_ignore_ascii_case("charset"))
            .then(|| value.trim().trim_matches('"').to_owned())
    })
}

async fn read_capped(mut response: Response) -> Result<Vec<u8>, reqwest::Error> {
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        body.extend_from_slice(&chunk);
        if body.len() >= MAX_BYTES {
            body.truncate(MAX_BYTES);
            break;
        }
    }
    Ok(body)
}

/// Seconds a Retry-After header asks for, as delta-seconds or an HTTP-date.
pub fn retry_after(value: Option<&str>) -> f64 {
    let Some(value) = value.map(str::trim) else {
        return DEFAULT_RETRY_AFTER;
    };
    if !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit()) {
        return value.parse().unwrap_or(DEFAULT_RETRY_AFTER);
    }
    match httpdate::parse_http_date(value) {
        Ok(when) => when.duration_since(SystemTime::now()).map_or(0.0, |left| left.as_secs_f64()),
        Err(_) => DEFAULT_RETRY_AFTER,
    }
}

#[cfg(test)]
mod tests {
    use std::net::IpAddr;

    use wiremock::matchers::any;
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::*;

    fn resolving_to(address: &'static str) -> Lookup {
        Arc::new(move |_| {
            Box::pin(async move { Ok(vec![address.parse::<IpAddr>().expect("an address")]) })
        })
    }

    fn redirecting_to(location: &str) -> ResponseTemplate {
        ResponseTemplate::new(302).insert_header("location", location)
    }

    fn options() -> Options {
        Options {
            contact: String::new(),
            host_interval: Duration::ZERO,
            render: false,
            stealth: false,
            max_age_hours: 48,
        }
    }

    // Each first request goes to a literal 127.0.0.1, which neither check sees, to reach a site at
    // all. What is under test is the hop it answers with.
    #[tokio::test]
    async fn a_redirect_to_a_private_address_is_not_followed() {
        let site = MockServer::start().await;
        let secret = format!("http://127.0.0.1:{}/secret", site.address().port());
        Mock::given(any()).respond_with(redirecting_to(&secret)).expect(1).mount(&site).await;

        let refused = client(
            Policy::custom(|attempt| redirect(attempt, Guard::Public)),
            Some(GuardedResolver(resolving_to("1.1.1.1"), Guard::Public)),
        )
        .get(format!("{}/start", site.uri()))
        .send()
        .await
        .unwrap_err();

        assert!(refused.is_redirect(), "{refused:?}");
    }

    #[tokio::test]
    async fn a_redirect_to_a_name_that_resolves_privately_is_not_connected() {
        let (first, second) = (MockServer::start().await, MockServer::start().await);
        let location = format!("http://inner.example:{}/secret", second.address().port());
        Mock::given(any()).respond_with(redirecting_to(&location)).mount(&first).await;
        Mock::given(any()).respond_with(ResponseTemplate::new(200)).expect(0).mount(&second).await;

        let refused = client(
            Policy::custom(|attempt| redirect(attempt, Guard::Public)),
            Some(GuardedResolver(resolving_to("127.0.0.1"), Guard::Public)),
        )
        .get(first.uri())
        .send()
        .await
        .unwrap_err();

        assert!(format!("{refused:?}").contains("not on the public internet"), "{refused:?}");
    }

    // The same hop with the name resolved by an unguarded client, to show the test above can tell.
    #[tokio::test]
    async fn the_name_in_that_redirect_does_reach_the_site_without_the_resolver() {
        let site = MockServer::start().await;
        Mock::given(any()).respond_with(ResponseTemplate::new(200)).mount(&site).await;
        let url = format!("http://localhost:{}/", site.address().port());
        assert!(client(Policy::none(), None).get(url).send().await.is_ok());
    }

    #[tokio::test]
    async fn a_redirect_of_robots_txt_to_a_private_address_denies_the_read() {
        let (first, second) = (MockServer::start().await, MockServer::start().await);
        let location = format!("{}/robots.txt", second.uri());
        Mock::given(any()).respond_with(redirecting_to(&location)).mount(&first).await;
        Mock::given(any()).respond_with(ResponseTemplate::new(200)).expect(0).mount(&second).await;
        let web = Web::new(options(), 4, Guard::Public, resolving_to("1.1.1.1"));

        assert_eq!(web.read_robots(&first.uri(), "127.0.0.1").await, None);
    }

    #[tokio::test]
    async fn a_name_that_rebinds_after_the_check_is_refused_at_the_connection() {
        let site = MockServer::start().await;
        Mock::given(any()).respond_with(ResponseTemplate::new(200)).expect(0).mount(&site).await;
        let answers = Arc::new(Mutex::new(vec!["127.0.0.1", "1.1.1.1"]));
        let lookup: Lookup = Arc::new(move |_| {
            let answer = answers.lock().unwrap().pop().expect("asked at most twice");
            Box::pin(async move { Ok(vec![answer.parse::<IpAddr>().expect("an address")]) })
        });
        let web = Web::new(options(), 4, Guard::Public, lookup);
        assert!(web.public("rebind.example").await);

        let url = format!("http://rebind.example:{}/", site.address().port());
        let failed = web.fetch_page(&url, None).await.unwrap_err();

        assert!(failed.to_string().contains("not on the public internet"), "{failed}");
    }

    #[tokio::test]
    async fn literal_addresses_are_refused_however_they_are_written() {
        let web = Web::new(options(), 4, Guard::Public, resolving_to("1.1.1.1"));
        for url in [
            "http://127.0.0.1/",
            "http://2130706433/",
            "http://0x7f.0.0.1/",
            "http://[::1]/",
            "http://[::ffff:10.0.0.1]/",
            "http://[64:ff9b::a00:1]/",
            "http://[2002:a00:1::]/",
            "http://169.254.169.254/latest/meta-data/",
            "ftp://example.org/",
            "file:///etc/passwd",
        ] {
            let failed = web.fetch_page(url, None).await.unwrap_err();
            assert!(failed.to_string().contains("not on the public internet"), "{url}: {failed}");
            assert!(!web.allowed(url).await, "{url}");
            assert!(!(web.page_check())(url.to_owned()).await, "{url}");
        }
    }

    #[tokio::test]
    async fn a_page_in_a_browser_cannot_ask_for_a_name_that_resolves_privately() {
        let private = Web::new(options(), 4, Guard::Public, resolving_to("10.0.0.1"));
        assert!(!(private.page_check())("https://inner.example/".to_owned()).await);
        let public = Web::new(options(), 4, Guard::Public, resolving_to("1.1.1.1"));
        assert!((public.page_check())("https://example.org/".to_owned()).await);
        assert!((public.page_check())("https://1.1.1.1/".to_owned()).await);
    }

    #[test]
    fn the_loopback_guard_lets_only_this_machine_through() {
        let allowed = |address: &str| Guard::Loopback.allows(address.parse().unwrap());
        assert!(allowed("127.0.0.1") && allowed("::1") && allowed("1.1.1.1"));
        assert!(!allowed("10.0.0.1") && !allowed("169.254.169.254") && !allowed("fe80::1"));
    }

    #[test]
    fn what_counts_as_public() {
        for private in [
            "0.0.0.0",
            "10.1.2.3",
            "100.64.0.1",
            "127.0.0.1",
            "169.254.169.254",
            "172.16.0.1",
            "192.168.1.1",
            "198.18.0.1",
            "224.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "::ffff:127.0.0.1",
            "::127.0.0.1",
            "64:ff9b::7f00:1",
            "2002:7f00:1::",
            "fc00::1",
            "fe80::1",
            "ff02::1",
            "2001:db8::1",
        ] {
            assert!(!is_public(private.parse().unwrap()), "{private}");
        }
        for public in ["1.1.1.1", "8.8.8.8", "172.32.0.1", "2606:4700::1111", "::ffff:1.1.1.1"] {
            assert!(is_public(public.parse().unwrap()), "{public}");
        }
    }

    #[test]
    fn retry_after_reads_seconds_and_dates() {
        assert_eq!(retry_after(Some("12")), 12.0);
        assert_eq!(retry_after(None), DEFAULT_RETRY_AFTER);
        assert_eq!(retry_after(Some("soon")), DEFAULT_RETRY_AFTER);
        assert_eq!(retry_after(Some("Wed, 21 Oct 2015 07:28:00 GMT")), 0.0);
        let later = httpdate::fmt_http_date(SystemTime::now() + Duration::from_secs(60));
        assert!((55.0..=60.0).contains(&retry_after(Some(&later))));
    }
}
