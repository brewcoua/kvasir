//! What the providers that read an engine's own pages share. These pages are not an API: their
//! selectors come from SearXNG's engines and may break without notice.

use std::time::Duration;

use reqwest::header::{
    ACCEPT, ACCEPT_LANGUAGE, COOKIE, HeaderMap, HeaderValue, SET_COOKIE, USER_AGENT,
};
use reqwest::redirect::Policy;

use super::{Cookies, TIMEOUT};

/// As SearXNG's own list has it. These engines refuse what does not look like a browser.
const BROWSER: &str = "Mozilla/5.0 (X11; Linux x86_64; rv:159.0) Gecko/20100101 Firefox/159.0";

/// Between two requests to an engine, which blocks a client that hurries.
pub(super) const INTERVAL: Duration = Duration::from_secs(2);

/// A client with a browser's headers, which leaves a redirect for the caller to read: some engines
/// block by redirecting.
pub(super) fn client() -> reqwest::Client {
    let mut headers = HeaderMap::new();
    headers.insert(USER_AGENT, HeaderValue::from_static(BROWSER));
    headers.insert(
        ACCEPT,
        HeaderValue::from_static("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    );
    headers.insert(ACCEPT_LANGUAGE, HeaderValue::from_static("en-US,en;q=0.5"));
    reqwest::Client::builder()
        .default_headers(headers)
        .redirect(Policy::none())
        .timeout(TIMEOUT)
        .build()
        .expect("the TLS backend starts")
}

/// `request` with what a person won clearing `provider`'s block, if they did.
pub(super) fn with_cookies(
    request: reqwest::RequestBuilder,
    cookies: &Cookies,
    provider: &str,
) -> reqwest::RequestBuilder {
    match cookies.get(provider) {
        Some(won) => request.header(COOKIE, won),
        None => request,
    }
}

/// A form's fields as the body of a POST.
pub(super) fn form(fields: &[(&str, &str)]) -> String {
    url::form_urlencoded::Serializer::new(String::new()).extend_pairs(fields).finish()
}

/// The cookies `response` sets, as a `Cookie` header would carry them.
pub(super) fn cookies(response: &reqwest::Response) -> Vec<(String, String)> {
    let set = response.headers().get_all(SET_COOKIE);
    set.iter()
        .filter_map(|header| {
            let (name, value) = header.to_str().ok()?.split(';').next()?.split_once('=')?;
            Some((name.trim().to_owned(), value.trim().to_owned()))
        })
        .collect()
}
