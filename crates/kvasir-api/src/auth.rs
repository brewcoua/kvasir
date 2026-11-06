//! One shared token, `KVASIR_TOKEN`, guards every route but health, the login and the page.
//!
//! A client sends `Authorization: Bearer <token>`. The page cannot on every request: `EventSource`
//! and `<img>` send no header. So `POST /v1/login` sets the token as a cookie, which is accepted too.
//! SameSite=Strict keeps another site's page from sending it, but a page on another port of the same
//! host is the same site. So a cookie-authenticated request that changes something is refused when
//! the browser says it comes from another origin.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::Extension;
use axum::body::Bytes;
use axum::extract::{ConnectInfo, Request, State};
use axum::http::{HeaderMap, Method, StatusCode, header};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use schemars::JsonSchema;
use serde::Deserialize;

use crate::{ApiError, App, body};

const COOKIE: &str = "kvasir_token";
// A browser left alone for a month logs in again.
const COOKIE_MAX_AGE_SECONDS: u64 = 30 * 24 * 3600;

#[derive(Deserialize, JsonSchema)]
pub(crate) struct Login {
    token: String,
}

/// Whether `given` is the token, in time that does not depend on where they first differ.
fn valid(app: &App, given: &str) -> bool {
    let token = app.settings().token.clone();
    let (given, token) = (given.as_bytes(), token.as_bytes());
    given.len() == token.len() && given.iter().zip(token).fold(0, |acc, (a, b)| acc | (a ^ b)) == 0
}

fn cookie(headers: &HeaderMap) -> Option<&str> {
    headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(';'))
        .filter_map(|pair| pair.trim().split_once('='))
        .find_map(|(name, value)| (name == COOKIE).then_some(value))
}

/// The layer every guarded route is behind.
pub async fn require_token(
    State(app): State<Arc<App>>,
    request: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let headers = request.headers();
    let authorization = headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok());
    let authorization = authorization.unwrap_or("");
    let (scheme, given) = authorization.split_once(' ').unwrap_or((authorization, ""));
    let given = if scheme.eq_ignore_ascii_case("bearer") {
        given
    } else {
        // `Sec-Fetch-Site` values of a request another page made. A client that sends none is no
        // browser.
        let site = headers.get("sec-fetch-site").and_then(|v| v.to_str().ok());
        let foreign = matches!(site, Some("same-site" | "cross-site"));
        let safe = matches!(*request.method(), Method::GET | Method::HEAD | Method::OPTIONS);
        if foreign && !safe {
            return Err(ApiError::Unauthorized);
        }
        cookie(headers).unwrap_or("")
    };
    if !valid(&app, given) {
        return Err(ApiError::Unauthorized);
    }
    Ok(next.run(request).await)
}

/// Sets the cookie the page authenticates with. 401 for a wrong token.
pub async fn login(
    State(app): State<Arc<App>>,
    peer: Option<Extension<ConnectInfo<SocketAddr>>>,
    headers: HeaderMap,
    sent: Bytes,
) -> Result<Response, ApiError> {
    let value = body::json(&sent)?;
    body::object(&value, &body::loc(&["body"]))?;
    let login: Login = body::decode(&value, &body::loc(&["body"]))?;
    if !valid(&app, &login.token) {
        return Err(ApiError::Unauthorized);
    }
    // Behind a proxy that ends TLS, the scheme is the one the proxy says, if it is trusted.
    let proto = headers.get("x-forwarded-proto").and_then(|v| v.to_str().ok());
    let https = proto.is_some_and(|proto| proto.split(',').next().map(str::trim) == Some("https"));
    let trusted = peer.is_some_and(|Extension(ConnectInfo(peer))| app.proxies.trusts(peer.ip()));
    let secure = if https && trusted { "; Secure" } else { "" };
    let cookie = format!(
        "{COOKIE}={}; HttpOnly; Max-Age={COOKIE_MAX_AGE_SECONDS}; Path=/; SameSite=strict{secure}",
        login.token
    );
    Ok((StatusCode::NO_CONTENT, [(header::SET_COOKIE, cookie)]).into_response())
}
