//! Mojeek's result page, read as SearXNG reads it, for whoever has no API key. It takes a cookie
//! won by solving an ALTCHA, a proof-of-work, which is slow enough that SearXNG leaves this off.

use std::sync::Mutex;
use std::time::Duration;

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use dom_query::Document;
use kvasir_llm::BoxFuture;
use reqwest::StatusCode;
use reqwest::header::{CONTENT_TYPE, COOKIE};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::Sha256;

use super::{Failure, Provider, Row, keyless_failure, one_line, other, scrape};

const BASE: &str = "https://www.mojeek.com";
const TOKEN_COOKIE: &str = "chllg";
const BOUNDARY: &str = "kvasir-altcha-boundary";
/// As SearXNG gives up.
const MOST_COUNTERS: u32 = 1000;
/// Bounds what an answer can make this compute, a long way above what such a challenge needs.
const MOST_COST: u32 = 1_000_000;
const LONGEST_KEY: usize = 64;

pub struct MojeekHtml {
    base: String,
    client: reqwest::Client,
    token: Mutex<Option<String>>,
}

impl Default for MojeekHtml {
    fn default() -> Self {
        Self::at(BASE)
    }
}

impl MojeekHtml {
    /// Only a test picks another address.
    pub fn at(base: &str) -> Self {
        Self { base: base.to_owned(), client: scrape::client(), token: Mutex::default() }
    }

    fn kept(&self) -> Option<String> {
        self.token.lock().expect("this lock is not held across a panic").clone()
    }

    fn keep(&self, token: Option<String>) {
        *self.token.lock().expect("this lock is not held across a panic") = token;
    }

    /// A new token, from a challenge solved off the async threads.
    async fn solve(&self) -> Result<String, Failure> {
        let url = format!("{}/captcha/challenge", self.base);
        let response = self.client.get(url).send().await.map_err(|e| other("request", e))?;
        let response = response.error_for_status().map_err(|e| other("challenge", e))?;
        let challenge: Value = response.json().await.map_err(|e| other("challenge", e))?;
        let parameters: Parameters = challenge
            .get("parameters")
            .cloned()
            .and_then(|parameters| serde_json::from_value(parameters).ok())
            .ok_or_else(|| Failure::Other("a challenge it cannot read".into()))?;
        let solved = tokio::task::spawn_blocking(move || parameters.solve())
            .await
            .map_err(|e| Failure::Other(format!("solving a challenge: {e}")))??;
        let answer = json!({
            "challenge": challenge,
            "solution": {"counter": solved.counter, "derivedKey": solved.key, "time": 150},
        });
        let response = self
            .client
            .post(format!("{}/captcha/verify", self.base))
            .header(CONTENT_TYPE, format!("multipart/form-data; boundary={BOUNDARY}"))
            .body(multipart(&STANDARD.encode(answer.to_string())))
            .send()
            .await
            .map_err(|e| other("request", e))?;
        let (_, token) = scrape::cookies(&response)
            .into_iter()
            .find(|(name, _)| name == TOKEN_COOKIE)
            .ok_or(Failure::Captcha)?;
        self.keep(Some(token.clone()));
        Ok(token)
    }

    /// The page for `query`, or `None` when the token is not accepted.
    async fn page(&self, query: &str, token: &str) -> Result<Option<String>, Failure> {
        let mut url = url::Url::parse(&self.base).expect("the base is a URL");
        url.set_path("/search");
        url.query_pairs_mut().append_pair("q", query).append_pair("safe", "0");
        let request = self.client.get(url).header(COOKIE, format!("{TOKEN_COOKIE}={token}"));
        let response = request.send().await.map_err(|e| other("request", e))?;
        if response.status() == StatusCode::FORBIDDEN {
            return Ok(None);
        }
        if let Some(failure) = keyless_failure(&response) {
            return Err(failure);
        }
        let response = response.error_for_status().map_err(|e| other("answer", e))?;
        let body = response.text().await.map_err(|e| other("answer", e))?;
        Ok((!body.contains("/captcha/challenge")).then_some(body))
    }

    async fn rows(&self, query: &str) -> Result<Vec<Row>, Failure> {
        // A kept token may have lapsed, which only asking shows.
        if let Some(token) = self.kept() {
            if let Some(page) = self.page(query, &token).await? {
                return read(&page);
            }
            self.keep(None);
        }
        let token = self.solve().await?;
        match self.page(query, &token).await? {
            Some(page) => read(&page),
            None => {
                self.keep(None);
                Err(Failure::Captcha)
            }
        }
    }
}

fn multipart(value: &str) -> String {
    format!(
        "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"altcha\"\r\n\r\n{value}\r\n\
         --{BOUNDARY}--\r\n"
    )
}

fn read(page: &str) -> Result<Vec<Row>, Failure> {
    let document = Document::from(page);
    let list = document.select("ul.results-standard");
    if !list.exists() {
        return Err(Failure::Other("no result list in the page".into()));
    }
    let results = list.select("li");
    Ok(results
        .iter()
        .filter_map(|result| {
            Some(Row {
                url: result.select("a.ob").attr("href")?.to_string(),
                title: one_line(&result.select("h2 a").text()),
                snippet: one_line(&result.select("p.s").text()),
            })
        })
        .collect())
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Parameters {
    algorithm: String,
    nonce: String,
    salt: String,
    key_prefix: String,
    cost: u32,
    key_length: usize,
}

struct Solved {
    counter: u32,
    key: String,
}

impl Parameters {
    fn solve(&self) -> Result<Solved, Failure> {
        let unreadable = || Failure::Other("a challenge it cannot read".into());
        if self.algorithm != "PBKDF2/SHA-256"
            || self.cost > MOST_COST
            || self.key_length > LONGEST_KEY
        {
            return Err(unreadable());
        }
        let nonce = from_hex(&self.nonce).ok_or_else(unreadable)?;
        let salt = from_hex(&self.salt).ok_or_else(unreadable)?;
        let prefix = from_hex(&self.key_prefix).ok_or_else(unreadable)?;
        let mut key = vec![0; self.key_length];
        for counter in 0..MOST_COUNTERS {
            let secret = [nonce.as_slice(), &counter.to_be_bytes()].concat();
            pbkdf2::pbkdf2_hmac::<Sha256>(&secret, &salt, self.cost, &mut key);
            if key.starts_with(&prefix) {
                let key = key.iter().map(|byte| format!("{byte:02x}")).collect();
                return Ok(Solved { counter, key });
            }
        }
        Err(Failure::Captcha)
    }
}

fn from_hex(text: &str) -> Option<Vec<u8>> {
    if !text.is_ascii() || !text.len().is_multiple_of(2) {
        return None;
    }
    (0..text.len()).step_by(2).map(|at| u8::from_str_radix(&text[at..at + 2], 16).ok()).collect()
}

impl Provider for MojeekHtml {
    fn name(&self) -> &'static str {
        "Mojeek HTML"
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

    fn parameters(counter: u32) -> Parameters {
        let mut key = [0; 4];
        let secret = [[0xab_u8].as_slice(), &counter.to_be_bytes()].concat();
        pbkdf2::pbkdf2_hmac::<Sha256>(&secret, &[0xcd], 2, &mut key);
        Parameters {
            algorithm: "PBKDF2/SHA-256".into(),
            nonce: "ab".into(),
            salt: "cd".into(),
            key_prefix: format!("{:02x}{:02x}", key[0], key[1]),
            cost: 2,
            key_length: 4,
        }
    }

    #[test]
    fn the_challenge_is_solved_at_the_counter_that_gives_the_prefix() {
        let parameters = parameters(3);

        let solved = parameters.solve().unwrap();

        assert_eq!(solved.counter, 3);
        assert!(solved.key.starts_with(&parameters.key_prefix));
        assert_eq!(solved.key.len(), 8);
    }

    #[test]
    fn a_challenge_outside_the_bounds_is_refused_without_computing() {
        let costly = Parameters { cost: MOST_COST + 1, ..parameters(0) };
        let long = Parameters { key_length: LONGEST_KEY + 1, ..parameters(0) };
        let other = Parameters { algorithm: "PBKDF2/SHA-1".into(), ..parameters(0) };

        for challenge in [costly, long, other] {
            assert!(matches!(challenge.solve(), Err(Failure::Other(_))));
        }
    }

    #[test]
    fn hex_is_read_or_refused() {
        assert_eq!(from_hex("00ff"), Some(vec![0, 255]));
        assert_eq!(from_hex("0"), None);
        assert_eq!(from_hex("zz"), None);
        assert_eq!(from_hex("é0"), None);
    }
}
