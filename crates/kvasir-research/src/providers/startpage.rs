//! Startpage's result page, read as SearXNG reads it. Against its terms: the operator turns this
//! on and carries that.
//!
//! ```text
//! GET /            -> the form's `sc` token, kept an hour
//! POST /sp/search  -> results as JSON inside the page's script
//! ```
//!
//! Either may answer with Anubis, a proof-of-work, which is solved here for a cookie.

use std::sync::Mutex;
use std::time::Duration;

use dom_query::Document;
use kvasir_llm::BoxFuture;
use reqwest::RequestBuilder;
use reqwest::header::{CONTENT_TYPE, COOKIE, LOCATION, ORIGIN, REFERER};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::time::Instant;

use super::{Failure, Provider, Row, keyless_failure, other, plain_text, scrape};

const BASE: &str = "https://www.startpage.com";
const TOKEN_LIFE: Duration = Duration::from_secs(3600);
const AUTH_LIFE: Duration = Duration::from_secs(240);
const AUTH_COOKIE: &str = "spchal-auth";
const CHALLENGE_MARK: &str = r#"id="anubis_challenge""#;
const CHALLENGE_OPEN: &str = r#"<script id="anubis_challenge" type="application/json">"#;
/// Startpage's is 4. Anything above is more work than a query is worth.
const MOST_DIFFICULT: u32 = 4;
const RESULTS_OPEN: &str = "React.createElement(UIStartpage.AppSerpWeb, {";
const RESULTS_CLOSE: &str = "}})";
const WEB_RESULTS: &str = "web-google";
const PREFERENCES: [(&str, &str); 14] = [
    ("date_time", "world"),
    ("disable_family_filter", "none"),
    ("disable_open_in_new_window", "0"),
    ("enable_post_method", "1"),
    ("enable_proxy_safety_suggest", "1"),
    ("enable_stay_control", "1"),
    ("instant_answers", "1"),
    ("lang_homepage", "s/device/en/"),
    ("num_of_results", "10"),
    ("suggestions", "1"),
    ("wt_unit", "celsius"),
    ("language", "english"),
    ("language_ui", "english"),
    ("search_results_region", "en_US"),
];

/// A value good until a time.
type Kept = Mutex<Option<(String, Instant)>>;

fn fresh(kept: &Kept) -> Option<String> {
    let kept = kept.lock().expect("this lock is not held across a panic");
    kept.as_ref().filter(|(_, until)| Instant::now() < *until).map(|(value, _)| value.clone())
}

fn keep(kept: &Kept, value: &str, life: Duration) {
    let until = Instant::now() + life;
    *kept.lock().expect("this lock is not held across a panic") = Some((value.to_owned(), until));
}

pub struct Startpage {
    base: String,
    client: reqwest::Client,
    token: Kept,
    auth: Kept,
}

impl Default for Startpage {
    fn default() -> Self {
        Self::at(BASE)
    }
}

/// A page and the cookies that came with it.
struct Page {
    body: String,
    cookies: Vec<(String, String)>,
}

impl Page {
    fn challenged(&self) -> bool {
        self.body.contains(CHALLENGE_MARK)
    }
}

impl Startpage {
    /// Only a test picks another address.
    pub fn at(base: &str) -> Self {
        Self {
            base: base.to_owned(),
            client: scrape::client(),
            token: Kept::default(),
            auth: Kept::default(),
        }
    }

    /// A challenge may come with any status, so it goes before the status is read.
    async fn page(request: RequestBuilder) -> Result<Page, Failure> {
        let response = request.send().await.map_err(|e| other("request", e))?;
        if let Some(failure) = keyless_failure(&response) {
            return Err(failure);
        }
        let to_captcha = response
            .headers()
            .get(LOCATION)
            .and_then(|location| location.to_str().ok())
            .is_some_and(|location| location.contains("/sp/captcha"));
        if to_captcha {
            return Err(Failure::Captcha);
        }
        let status = response.status();
        let cookies = scrape::cookies(&response);
        let body = response.text().await.map_err(|e| other("answer", e))?;
        let page = Page { body, cookies };
        if !status.is_success() && !page.challenged() {
            return Err(Failure::Other(format!("answered with {status}")));
        }
        Ok(page)
    }

    /// The solved challenge's cookie, kept for later requests.
    async fn pass(&self, challenged: &Page, from: &str) -> Result<String, Failure> {
        let (id, nonce, digest) = solve(&challenged.body).await?;
        let mut url = url::Url::parse(&self.base).expect("the base is a URL");
        url.set_path("/.within.website/x/cmd/anubis/api/pass-challenge");
        url.query_pairs_mut()
            .append_pair("id", &id)
            .append_pair("response", &digest)
            .append_pair("nonce", &nonce.to_string())
            .append_pair("redir", from)
            .append_pair("elapsedTime", "1");
        let cookie = challenged
            .cookies
            .iter()
            .map(|(name, value)| format!("{name}={value}"))
            .collect::<Vec<_>>()
            .join("; ");
        let answer = self
            .client
            .get(url)
            .header(COOKIE, cookie)
            .send()
            .await
            .map_err(|e| other("request", e))?;
        let (_, auth) = scrape::cookies(&answer)
            .into_iter()
            .find(|(name, _)| name == AUTH_COOKIE)
            .ok_or(Failure::Captcha)?;
        keep(&self.auth, &auth, AUTH_LIFE);
        Ok(auth)
    }

    /// `build`'s request with the auth cookie if there is one, and once more if it is challenged.
    async fn through_challenge(
        &self,
        from: &str,
        build: impl Fn(String) -> RequestBuilder,
    ) -> Result<String, Failure> {
        let page = Self::page(build(cookie_header(fresh(&self.auth).as_deref()))).await?;
        if !page.challenged() {
            return Ok(page.body);
        }
        let auth = self.pass(&page, from).await?;
        let page = Self::page(build(cookie_header(Some(&auth)))).await?;
        if page.challenged() {
            return Err(Failure::Captcha);
        }
        Ok(page.body)
    }

    async fn token(&self) -> Result<String, Failure> {
        if let Some(token) = fresh(&self.token) {
            return Ok(token);
        }
        let home = format!("{}/", self.base);
        let build = |cookie| self.client.get(&home).header(COOKIE, cookie);
        let body = self.through_challenge(&home, build).await?;
        let token = {
            let document = Document::from(body);
            let field = document.select(r#"form#search input[name="sc"]"#);
            // Without it Startpage takes the request for a bot, which is how SearXNG treats this.
            field.attr("value").ok_or(Failure::Captcha)?.to_string()
        };
        keep(&self.token, &token, TOKEN_LIFE);
        Ok(token)
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        let token = self.token().await?;
        let url = format!("{}/sp/search", self.base);
        let form = scrape::form(&[
            ("query", query),
            ("cat", "web"),
            ("t", "device"),
            ("sc", &token),
            ("with_date", ""),
            ("abd", "1"),
            ("abe", "1"),
            ("qsr", "all"),
            ("qadf", "none"),
            ("language", "english"),
            ("lui", "english"),
            ("segment", "startpage.udog"),
        ]);
        let build = |cookie| {
            self.client
                .post(&url)
                .header(CONTENT_TYPE, "application/x-www-form-urlencoded")
                .header(ORIGIN, &self.base)
                .header(REFERER, format!("{}/", self.base))
                .header(COOKIE, cookie)
                .body(form.clone())
        };
        read(&self.through_challenge(&url, build).await?)
    }
}

/// Its preferences, which a search needs to look like the form's, and the auth cookie.
fn cookie_header(auth: Option<&str>) -> String {
    let preferences = PREFERENCES
        .iter()
        .map(|(name, value)| format!("{name}EEE{value}"))
        .collect::<Vec<_>>()
        .join("N1N");
    match auth {
        Some(auth) => format!("preferences={preferences}; {AUTH_COOKIE}={auth}"),
        None => format!("preferences={preferences}"),
    }
}

fn between<'a>(text: &'a str, open: &str, close: &str) -> Option<&'a str> {
    let (_, rest) = text.split_once(open)?;
    Some(rest.split_once(close)?.0)
}

fn read(page: &str) -> Result<Vec<Row>, Failure> {
    let no_results = || Failure::Other("no results in the page".into());
    let inner = between(page, RESULTS_OPEN, RESULTS_CLOSE).ok_or_else(no_results)?;
    let answer: Value =
        serde_json::from_str(&format!("{{{inner}}}}}")).map_err(|_| no_results())?;
    let mainline = answer.pointer("/render/presenter/regions/mainline").and_then(Value::as_array);
    let text = |item: &Value, field: &str| {
        item.get(field).and_then(Value::as_str).map_or_else(String::new, plain_text)
    };
    Ok(mainline
        .into_iter()
        .flatten()
        .filter(|group| group.get("display_type").and_then(Value::as_str) == Some(WEB_RESULTS))
        .filter_map(|group| group.get("results")?.as_array())
        .flatten()
        .filter_map(|item| {
            Some(Row {
                url: item.get("clickUrl")?.as_str()?.to_owned(),
                title: text(item, "title"),
                snippet: text(item, "description"),
            })
        })
        .collect())
}

#[derive(Deserialize)]
struct Anubis {
    challenge: Challenge,
    rules: Rules,
}

#[derive(Deserialize)]
struct Challenge {
    id: String,
    #[serde(rename = "randomData")]
    random_data: String,
}

#[derive(Deserialize)]
struct Rules {
    difficulty: u32,
}

/// The challenge's id, and the nonce and digest that answer it.
async fn solve(page: &str) -> Result<(String, u64, String), Failure> {
    let payload = between(page, CHALLENGE_OPEN, "</script>")
        .ok_or_else(|| Failure::Other("a challenge it cannot read".into()))?;
    let anubis: Anubis = serde_json::from_str(payload)
        .map_err(|_| Failure::Other("a challenge it cannot read".into()))?;
    if anubis.rules.difficulty > MOST_DIFFICULT {
        return Err(Failure::Captcha);
    }
    let Challenge { id, random_data } = anubis.challenge;
    let difficulty = anubis.rules.difficulty;
    let answer = tokio::task::spawn_blocking(move || work(&random_data, difficulty))
        .await
        .map_err(|e| Failure::Other(format!("solving a challenge: {e}")))?;
    let (nonce, digest) = answer.ok_or(Failure::Captcha)?;
    Ok((id, nonce, digest))
}

/// A nonce whose hash with `data` starts with `difficulty` zero digits. Gives up at 8 times the
/// average search, which fails 0.03% of the time.
fn work(data: &str, difficulty: u32) -> Option<(u64, String)> {
    let zeros = "0".repeat(difficulty as usize);
    (0..16_u64.pow(difficulty) * 8).find_map(|nonce| {
        let digest = format!("{:x}", Sha256::digest(format!("{data}{nonce}")));
        digest.starts_with(&zeros).then_some((nonce, digest))
    })
}

impl Provider for Startpage {
    fn name(&self) -> &'static str {
        "Startpage"
    }

    fn interval(&self) -> Duration {
        scrape::INTERVAL
    }

    fn query<'a>(&'a self, query: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
        Box::pin(self.rows(query))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_work_finds_a_hash_with_the_zeros() {
        let (nonce, digest) = work("data", 2).unwrap();

        assert!(digest.starts_with("00"), "{digest}");
        assert_eq!(digest, format!("{:x}", Sha256::digest(format!("data{nonce}"))));
    }
}
