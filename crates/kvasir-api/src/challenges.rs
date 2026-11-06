//! Search providers' blocks a person can clear: the list, and with the `chromium` feature, the tab
//! to clear one in.
//!
//! ```text
//! GET /v1/search/challenges                  what waits for a person
//! GET /v1/search/challenges/{provider}/live  a WebSocket on a browser tab on this machine
//! ```
//!
//! The socket sends `{"frame": <base64 JPEG>}` as the tab changes, and at the end `{"state": ...}`,
//! one of `cleared`, `expired`, `left` and `failed`. It takes [`kvasir_browser::Input`] as JSON.

use std::sync::Arc;

use axum::Json;
use axum::extract::State;
use axum::routing::get;
use schemars::JsonSchema;
use serde::Serialize;

use crate::App;

pub fn routes() -> axum::Router<Arc<App>> {
    let routes = axum::Router::new().route("/v1/search/challenges", get(list));
    #[cfg(feature = "chromium")]
    let routes = routes.route("/v1/search/challenges/{provider}/live", get(live::open));
    routes
}

#[derive(Serialize, JsonSchema)]
pub struct ChallengeInfo {
    provider: String,
    url: String,
    expires_in: f64,
}

/// Without a browser none can be cleared, so none is offered: the provider just rests.
async fn list(State(app): State<Arc<App>>) -> Json<Vec<ChallengeInfo>> {
    let challenges =
        if cfg!(feature = "chromium") { app.live().search.challenges() } else { vec![] };
    Json(
        challenges
            .into_iter()
            .map(|challenge| ChallengeInfo {
                provider: challenge.provider.to_owned(),
                expires_in: challenge.remaining().as_secs_f64(),
                url: challenge.url,
            })
            .collect(),
    )
}

#[cfg(feature = "chromium")]
mod live {
    use std::time::Duration;

    use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
    use axum::extract::{Path, State};
    use axum::http::{HeaderMap, StatusCode, header};
    use axum::response::Response;
    use futures::StreamExt;
    use kvasir_browser::{Input, Tab};
    use kvasir_research::providers::{Challenge, Providers};
    use serde_json::json;

    use super::*;
    use crate::ApiError;

    const CHECK_EVERY: Duration = Duration::from_secs(1);

    /// A handshake is a GET, which the token's cookie is good for even from a page on another port
    /// of this host. It drives a real browser, so such a page must not reach it.
    fn from_this_page(headers: &HeaderMap) -> bool {
        let value = |name| headers.get(name).and_then(|value| value.to_str().ok());
        match value("sec-fetch-site") {
            Some(site) => matches!(site, "same-origin" | "none"),
            None => value(header::ORIGIN.as_str()).is_none_or(|origin| {
                let origin = origin.split_once("://").map_or(origin, |(_, host)| host);
                value(header::HOST.as_str()) == Some(origin)
            }),
        }
    }

    pub(super) async fn open(
        State(app): State<Arc<App>>,
        Path(provider): Path<String>,
        headers: HeaderMap,
        upgrade: WebSocketUpgrade,
    ) -> Result<Response, ApiError> {
        if !from_this_page(&headers) {
            return Err(ApiError::new(StatusCode::FORBIDDEN, "Not from this page"));
        }
        let search = Arc::clone(&app.live().search);
        let challenge = search.challenges().into_iter().find(|c| c.provider == provider);
        let Some(challenge) = challenge else {
            return Err(ApiError::new(StatusCode::NOT_FOUND, "No such challenge"));
        };
        Ok(upgrade.on_upgrade(move |socket| session(socket, app, search, challenge)))
    }

    async fn session(
        mut socket: WebSocket,
        app: Arc<App>,
        search: Arc<Providers>,
        challenge: Challenge,
    ) {
        let within = challenge.clone();
        let stays = Arc::new(move |url: &str| within.within(url));
        let state = match Tab::open(&challenge.url, app.web.page_check(), stays).await {
            Ok(tab) => {
                let state = drive(&mut socket, &tab, &app, &search, &challenge).await;
                tab.close().await;
                state
            }
            Err(err) => {
                tracing::error!("cannot open {}'s challenge: {err}", challenge.provider);
                "failed"
            }
        };
        let end = json!({"state": state}).to_string();
        socket.send(Message::text(end)).await.ok();
    }

    /// Passes frames and input between the tab and the socket until the block is gone or the
    /// session ends, and says how it ended.
    async fn drive(
        socket: &mut WebSocket,
        tab: &Tab,
        app: &App,
        search: &Providers,
        challenge: &Challenge,
    ) -> &'static str {
        let Ok(frames) = tab.frames().await else { return "failed" };
        let mut frames = std::pin::pin!(frames);
        let mut check = tokio::time::interval(CHECK_EVERY);
        let expiry = tokio::time::sleep(challenge.remaining());
        let mut expiry = std::pin::pin!(expiry);
        loop {
            tokio::select! {
                () = &mut expiry => return "expired",
                () = tab.left() => return "left",
                frame = frames.next() => {
                    let Some(frame) = frame else { return "failed" };
                    let frame = json!({"frame": frame}).to_string();
                    if socket.send(Message::text(frame)).await.is_err() {
                        return "failed";
                    }
                }
                sent = socket.recv() => match sent {
                    Some(Ok(Message::Text(text))) => {
                        match serde_json::from_str::<Input>(&text) {
                            Ok(input) => {
                                if tab.input(input).await.is_err() {
                                    return "failed";
                                }
                            }
                            Err(err) => tracing::warn!("an input it cannot read: {err}"),
                        }
                    }
                    Some(Ok(_)) => {}
                    _ => return "failed",
                },
                _ = check.tick() => {
                    if let Ok(Some((url, html))) = tab.loaded().await
                        && challenge.cleared(&url, &html)
                    {
                        return clear(tab, app, search, challenge).await;
                    }
                }
            }
        }
    }

    async fn clear(
        tab: &Tab,
        app: &App,
        search: &Providers,
        challenge: &Challenge,
    ) -> &'static str {
        let Ok(cookies) = tab.cookies().await else { return "failed" };
        if !cookies.is_empty() {
            let provider = challenge.provider.to_owned();
            if let Err(err) =
                kvasir_config::store::set_search_cookies(&app.db, provider, cookies.clone()).await
            {
                tracing::error!("cannot keep {}'s cookies: {err}", challenge.provider);
            }
        }
        search.cleared(challenge.provider, cookies);
        "cleared"
    }

    #[cfg(test)]
    mod tests {
        use std::collections::HashSet;

        use futures::SinkExt;
        use kvasir_fetch::Guard;
        use kvasir_llm::BoxFuture;
        use kvasir_research::Search;
        use kvasir_research::providers::{Clearance, Failure, Provider, Row, State};
        use kvasir_run::{Run, RunCtx};
        use tokio_tungstenite::tungstenite::client::IntoClientRequest;
        use tokio_tungstenite::tungstenite::{self, Message as Sent};
        use wiremock::matchers::path;
        use wiremock::{Mock, MockServer, ResponseTemplate};

        use super::*;
        use crate::{Live, Proxies, router};

        const BLOCK: &str = r#"<body style="margin:0"><title>captcha</title>
            <button style="width:400px;height:400px"
              onclick="document.cookie='won=yes; path=/'; document.title='fine'">Solve</button>
            </body>"#;

        struct Blocked(String);

        impl Provider for Blocked {
            fn name(&self) -> &'static str {
                "Engine"
            }

            fn clearance(&self, _: &str) -> Option<Clearance> {
                Some(Clearance {
                    url: self.0.clone(),
                    blocked: |_, html| html.contains("<title>captcha</title>"),
                })
            }

            fn query<'a>(&'a self, _: &'a str) -> BoxFuture<'a, Result<Vec<Row>, Failure>> {
                Box::pin(async { Err(Failure::Captcha) })
            }
        }

        fn request(address: &str, site: &str) -> tungstenite::handshake::client::Request {
            let url = format!("ws://{address}/v1/search/challenges/Engine/live");
            let mut request = url.into_client_request().unwrap();
            let headers = request.headers_mut();
            headers.insert("cookie", "kvasir_token=the-token".parse().unwrap());
            headers.insert("sec-fetch-site", site.parse().unwrap());
            request
        }

        #[tokio::test]
        #[ignore = "needs Chromium"]
        async fn a_person_clears_a_block_through_the_socket() {
            let site = MockServer::start().await;
            Mock::given(path("/block"))
                .respond_with(ResponseTemplate::new(200).set_body_raw(BLOCK, "text/html"))
                .mount(&site)
                .await;
            let dir = tempfile::tempdir().unwrap();
            let env = kvasir_config::Settings::from_env(
                [
                    ("KVASIR_TOKEN".to_owned(), "the-token".to_owned()),
                    ("KVASIR_SECRET_KEY".to_owned(), "ab".repeat(32)),
                    ("KVASIR_DATA_DIR".to_owned(), dir.path().display().to_string()),
                ],
                &mut vec![],
            )
            .unwrap();
            let app = crate::open(env, Guard::Loopback, Proxies::parse(None), None).await.unwrap();
            let search = Arc::new(Providers::new(
                vec![(Box::new(Blocked(format!("{}/block", site.uri()))), 1.0)],
                Arc::clone(&app.web),
                Arc::clone(&app.search_cookies),
            ));
            let live = app.live();
            app.live.store(Arc::new(Live {
                settings: Arc::clone(&live.settings),
                favicons: live.favicons.clone(),
                sources: live.sources.clone(),
                search: Arc::clone(&search),
            }));
            let ctx = RunCtx::new(Arc::new(Run::new("ledger", "topic", Vec::new(), false)));
            search.search("q", &HashSet::new(), &ctx).await;
            assert_eq!(search.challenges().len(), 1);

            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address = listener.local_addr().unwrap().to_string();
            let served = router(Arc::clone(&app));
            tokio::spawn(async move { axum::serve(listener, served).await });

            let mut anonymous = request(&address, "same-origin");
            anonymous.headers_mut().remove("cookie");
            let refused = tokio_tungstenite::connect_async(anonymous).await;
            assert!(
                matches!(&refused, Err(tungstenite::Error::Http(answer)) if answer.status() == 401)
            );

            // Another port of this host is the same site, and its page must not get in.
            let refused = tokio_tungstenite::connect_async(request(&address, "same-site")).await;
            assert!(
                matches!(&refused, Err(tungstenite::Error::Http(answer)) if answer.status() == 403)
            );

            let (mut socket, _) =
                tokio_tungstenite::connect_async(request(&address, "same-origin")).await.unwrap();
            let mut clicked = false;
            let mut state = None;
            while let Some(sent) = socket.next().await {
                let Sent::Text(text) = sent.unwrap() else { continue };
                let message: serde_json::Value = serde_json::from_str(&text).unwrap();
                if let Some(ended) = message.get("state") {
                    state = Some(ended.as_str().unwrap().to_owned());
                    break;
                }
                assert!(message["frame"].as_str().unwrap().starts_with("/9j/"));
                if !clicked {
                    clicked = true;
                    for action in ["press", "release"] {
                        let input = json!({"type": "mouse", "action": action, "x": 50, "y": 50, "pressed": action == "press"});
                        socket.send(Sent::text(input.to_string())).await.unwrap();
                    }
                }
            }

            assert_eq!(state.as_deref(), Some("cleared"));
            assert!(search.challenges().is_empty());
            assert_eq!(search.statuses()[0].state, State::Ok);
            assert_eq!(app.search_cookies.get("Engine").as_deref(), Some("won=yes"));
            let kept = kvasir_config::store::search_cookies(&app.db).await.unwrap();
            assert_eq!(kept, [("Engine".to_owned(), "won=yes".to_owned())]);
        }

        #[test]
        fn only_this_page_may_open_a_socket() {
            let headers = |pairs: &[(&'static str, &'static str)]| {
                let mut headers = HeaderMap::new();
                for (name, value) in pairs {
                    headers.insert(*name, value.parse().unwrap());
                }
                headers
            };

            assert!(from_this_page(&headers(&[("sec-fetch-site", "same-origin")])));
            assert!(!from_this_page(&headers(&[("sec-fetch-site", "same-site")])));
            assert!(!from_this_page(&headers(&[("sec-fetch-site", "cross-site")])));
            // No browser sends neither.
            assert!(from_this_page(&headers(&[])));
            assert!(from_this_page(&headers(&[("origin", "http://a:1"), ("host", "a:1")])));
            assert!(!from_this_page(&headers(&[("origin", "http://a:2"), ("host", "a:1")])));
        }
    }
}
