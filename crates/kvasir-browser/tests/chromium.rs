//! Against a real Chromium: `cargo test -p kvasir-browser --features chromium -- --ignored`.
#![cfg(feature = "chromium")]

use std::sync::Arc;

use kvasir_browser::{Allow, Chromium, Render};
use wiremock::matchers::path;
use wiremock::{Mock, MockServer, ResponseTemplate};

fn html(body: String) -> ResponseTemplate {
    ResponseTemplate::new(200).set_body_raw(body, "text/html")
}

fn only_ip() -> Allow {
    Arc::new(|url| Box::pin(async move { url.starts_with("http://127.0.0.1") }))
}

async fn serve(route: &str, body: String) -> MockServer {
    let server = MockServer::start().await;
    Mock::given(path(route)).respond_with(html(body)).mount(&server).await;
    server
}

#[tokio::test]
#[ignore = "needs Chromium"]
async fn a_script_built_page_is_read_and_its_controls_removed() {
    let body = r#"<body><button>Press</button><div id="x"></div>
        <div style="display:none">Hidden</div>
        <script>document.getElementById('x').innerHTML = '<p>Written by a script.</p>'</script></body>"#;
    let server = serve("/a", body.to_owned()).await;
    let url = format!("{}/a", server.uri());

    let got = Chromium::new(true, only_ip()).render(&url).await.unwrap();

    assert_eq!(got.status, 200);
    assert_eq!(got.final_url, url);
    assert!(got.html.contains("Written by a script."));
    assert!(!got.html.contains("Press"));
    assert!(!got.html.contains("Hidden"));
}

#[tokio::test]
#[ignore = "needs Chromium"]
async fn an_error_status_is_reported() {
    let server = MockServer::start().await;
    Mock::given(path("/gone"))
        .respond_with(ResponseTemplate::new(404).set_body_raw("<p>No</p>", "text/html"))
        .mount(&server)
        .await;
    let got = Chromium::new(false, only_ip()).render(&format!("{}/gone", server.uri())).await;
    assert_eq!(got.unwrap().status, 404);
}

#[tokio::test]
#[ignore = "needs Chromium"]
async fn a_frame_or_a_script_cannot_reach_what_is_not_allowed() {
    let private = MockServer::start().await;
    Mock::given(path("/secret"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&private)
        .await;
    // `localhost` is another site than the page's 127.0.0.1, so this frame is cross-origin.
    let target = private.uri().replace("127.0.0.1", "localhost");
    let body = format!(
        r#"<body><p>Article.</p><iframe src="{target}/secret"></iframe>
        <script>fetch("{target}/secret", {{mode: "no-cors"}}).catch(() => {{}})</script></body>"#
    );
    let server = serve("/a", body).await;

    let got = Chromium::new(true, only_ip()).render(&format!("{}/a", server.uri())).await;

    assert!(got.unwrap().html.contains("Article."));
    private.verify().await;
}

#[tokio::test]
#[ignore = "needs Chromium"]
async fn runtime_enable_cannot_be_noticed() {
    // What spike 3 found tells: the protocol serializing console arguments, which a page can time.
    let body = r#"<body><p id="out"></p><script>
        const big = Array.from({length: 200000}, (_, i) => ({i}));
        const start = performance.now();
        for (let i = 0; i < 50; i++) console.debug(big);
        document.getElementById('out').textContent = 'took ' + (performance.now() - start);
        </script></body>"#;
    let server = serve("/a", body.to_owned()).await;

    let got = Chromium::new(true, only_ip()).render(&format!("{}/a", server.uri())).await.unwrap();

    let took = got.html.split("took ").nth(1).unwrap();
    let took: f64 = took.split('<').next().unwrap().parse().unwrap();
    assert!(took < 50.0, "{took} ms");
}

mod tab {
    use std::time::Duration;

    use futures::StreamExt;
    use kvasir_browser::{Input, Tab};
    use tokio::time::timeout;

    use super::*;

    const BLOCK: &str = r#"<body style="margin:0">
        <button id="b" style="width:400px;height:400px"
          onclick="document.cookie='won=yes; path=/'; document.title='cleared'">Solve</button>
        <input id="t" autofocus>
        </body>"#;

    fn stay_on(host: String) -> Arc<dyn Fn(&str) -> bool + Send + Sync> {
        Arc::new(move |url| url.starts_with(&host))
    }

    fn click(x: f64, y: f64) -> [Input; 2] {
        let press = serde_json::json!({"type": "mouse", "action": "press", "x": x, "y": y, "pressed": true});
        let release = serde_json::json!({"type": "mouse", "action": "release", "x": x, "y": y});
        [serde_json::from_value(press).unwrap(), serde_json::from_value(release).unwrap()]
    }

    #[tokio::test]
    #[ignore = "needs Chromium"]
    async fn a_person_sees_the_tab_drives_it_and_its_cookies_are_read() {
        let server = serve("/block", BLOCK.to_owned()).await;
        let tab = Tab::open(&format!("{}/block", server.uri()), only_ip(), stay_on(server.uri()))
            .await
            .unwrap();
        assert!(tab.loaded().await.unwrap().unwrap().1.contains("Solve"));

        let mut frames = Box::pin(tab.frames().await.unwrap());
        let frame = timeout(Duration::from_secs(10), frames.next()).await.unwrap().unwrap();
        // A JPEG in base64 starts with the encoding of its magic number.
        assert!(frame.starts_with("/9j/"), "{}", &frame[..8]);

        for event in click(100.0, 100.0) {
            tab.input(event).await.unwrap();
        }
        let typed = serde_json::json!({"type": "text", "text": "typed"});
        tab.input(serde_json::from_value(typed).unwrap()).await.unwrap();

        assert_eq!(tab.cookies().await.unwrap(), "won=yes");
        let (url, html) = tab.loaded().await.unwrap().unwrap();
        assert_eq!(url, format!("{}/block", server.uri()));
        assert!(html.contains("<title>cleared</title>"), "{html}");

        drop(frames);
        tab.close().await;
    }

    #[tokio::test]
    #[ignore = "needs Chromium"]
    async fn the_tab_cannot_leave_the_site() {
        let other = serve("/elsewhere", "<p>Elsewhere</p>".to_owned()).await;
        let leave = format!("{}/elsewhere", other.uri());
        let body = format!("<script>setTimeout(() => location.href = '{leave}', 300)</script>");
        let server = serve("/block", body).await;
        let tab = Tab::open(&format!("{}/block", server.uri()), only_ip(), stay_on(server.uri()))
            .await
            .unwrap();

        timeout(Duration::from_secs(10), tab.left()).await.expect("it told");
        assert!(other.received_requests().await.unwrap().is_empty());
        tab.close().await;
    }
}
