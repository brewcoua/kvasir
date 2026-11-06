//! Each provider against a fake of its documented request and answer, including its errors.

use std::sync::Arc;
use std::time::Duration;

use kvasir_fetch::{Guard, Options, Web, system_lookup};
use kvasir_research::providers::{
    Arxiv, Brave, Exa, Failure, Mojeek, Provider, Row, Tavily, Wikipedia,
};
use serde_json::json;
use wiremock::matchers::{header, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn row(url: &str, title: &str, snippet: &str) -> Row {
    Row { url: url.into(), title: title.into(), snippet: snippet.into() }
}

/// A server answering every request with `answer`.
async fn answering(answer: ResponseTemplate) -> MockServer {
    let server = MockServer::start().await;
    Mock::given(path("/s")).respond_with(answer).mount(&server).await;
    server
}

fn web() -> Arc<Web> {
    let options = Options {
        contact: "ops@example.org".into(),
        host_interval: Duration::ZERO,
        render: false,
        stealth: false,
        max_age_hours: 48,
    };
    Arc::new(Web::new(options, 8, Guard::Public, system_lookup()))
}

mod brave {
    use super::*;

    fn at(server: &MockServer) -> Brave {
        Brave::at(&format!("{}/s", server.uri()), "the-key")
    }

    #[tokio::test]
    async fn results_are_read_from_the_request_the_documentation_gives() {
        let server = MockServer::start().await;
        let answer = json!({"web": {"results": [
            {"url": "https://a.org", "title": "A", "description": "about a"},
            {"url": "https://b.org"},
        ]}});
        Mock::given(method("GET"))
            .and(path("/s"))
            .and(header("X-Subscription-Token", "the-key"))
            .and(query_param("q", "the query"))
            .and(query_param("count", "20"))
            .respond_with(ResponseTemplate::new(200).set_body_json(answer))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server).query("the query").await.unwrap();

        assert_eq!(rows, [row("https://a.org", "A", "about a"), row("https://b.org", "", "")]);
    }

    #[tokio::test]
    async fn an_answer_without_web_results_is_empty() {
        let server = answering(ResponseTemplate::new(200).set_body_json(json!({}))).await;

        assert_eq!(at(&server).query("q").await, Ok(Vec::new()));
    }

    #[tokio::test]
    async fn failures_are_told_apart() {
        for (status, want) in [
            (401, Failure::Unauthorized),
            (403, Failure::Unauthorized),
            (429, Failure::RateLimited { retry_after: None }),
        ] {
            let server = answering(ResponseTemplate::new(status)).await;
            assert_eq!(at(&server).query("q").await, Err(want), "{status}");
        }

        let limited = ResponseTemplate::new(429).insert_header("retry-after", "7");
        let server = answering(limited).await;
        let retry_after = Some(Duration::from_secs(7));
        assert_eq!(at(&server).query("q").await, Err(Failure::RateLimited { retry_after }));

        let server = answering(ResponseTemplate::new(500)).await;
        assert!(matches!(at(&server).query("q").await, Err(Failure::Other(_))));
    }
}

mod tavily {
    use super::*;
    use wiremock::matchers::body_json;

    fn at(server: &MockServer) -> Tavily {
        Tavily::at(&format!("{}/s", server.uri()), "the-key")
    }

    #[tokio::test]
    async fn results_are_read_from_the_request_the_documentation_gives() {
        let server = MockServer::start().await;
        let answer = json!({"results": [
            {"url": "https://a.org", "title": "A", "content": "about a", "score": 0.5},
        ]});
        Mock::given(method("POST"))
            .and(path("/s"))
            .and(header("authorization", "Bearer the-key"))
            .and(body_json(json!({"query": "the query", "max_results": 20})))
            .respond_with(ResponseTemplate::new(200).set_body_json(answer))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server).query("the query").await.unwrap();

        assert_eq!(rows, [row("https://a.org", "A", "about a")]);
    }

    #[tokio::test]
    async fn failures_are_told_apart() {
        for (status, want) in [
            (401, Failure::Unauthorized),
            (432, Failure::QuotaExhausted),
            (433, Failure::QuotaExhausted),
            (429, Failure::RateLimited { retry_after: None }),
        ] {
            let server = answering(ResponseTemplate::new(status)).await;
            assert_eq!(at(&server).query("q").await, Err(want), "{status}");
        }

        let limited = ResponseTemplate::new(429).insert_header("retry-after", "7");
        let server = answering(limited).await;
        let retry_after = Some(Duration::from_secs(7));
        assert_eq!(at(&server).query("q").await, Err(Failure::RateLimited { retry_after }));
    }
}

mod exa {
    use super::*;
    use wiremock::matchers::body_json;

    fn at(server: &MockServer) -> Exa {
        Exa::at(&format!("{}/s", server.uri()), "the-key")
    }

    #[tokio::test]
    async fn results_are_read_from_the_request_the_documentation_gives() {
        let server = MockServer::start().await;
        let answer = json!({"results": [
            {"url": "https://a.org", "title": "A", "highlights": ["one", "two"]},
            {"url": "https://b.org", "title": null},
        ]});
        let sent =
            json!({"query": "the query", "numResults": 20, "contents": {"highlights": true}});
        Mock::given(method("POST"))
            .and(path("/s"))
            .and(header("x-api-key", "the-key"))
            .and(body_json(sent))
            .respond_with(ResponseTemplate::new(200).set_body_json(answer))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server).query("the query").await.unwrap();

        assert_eq!(rows, [row("https://a.org", "A", "one two"), row("https://b.org", "", "")]);
    }

    #[tokio::test]
    async fn failures_are_told_apart() {
        for (status, want) in [
            (401, Failure::Unauthorized),
            (402, Failure::QuotaExhausted),
            (429, Failure::RateLimited { retry_after: None }),
        ] {
            let server = answering(ResponseTemplate::new(status)).await;
            assert_eq!(at(&server).query("q").await, Err(want), "{status}");
        }
    }
}

mod mojeek {
    use super::*;

    fn at(server: &MockServer) -> Mojeek {
        Mojeek::at(&format!("{}/s", server.uri()), "the-key")
    }

    #[tokio::test]
    async fn results_are_read_from_the_request_the_documentation_gives() {
        let server = MockServer::start().await;
        let answer = json!({"response": {"status": "OK", "results": [
            {"url": "https://a.org", "title": "A", "desc": "about a", "score": 3},
        ]}});
        Mock::given(method("GET"))
            .and(path("/s"))
            .and(query_param("fmt", "json"))
            .and(query_param("api_key", "the-key"))
            .and(query_param("q", "the query"))
            .and(query_param("t", "20"))
            .respond_with(ResponseTemplate::new(200).set_body_json(answer))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server).query("the query").await.unwrap();

        assert_eq!(rows, [row("https://a.org", "A", "about a")]);
    }

    #[tokio::test]
    async fn an_error_comes_in_the_status() {
        let status = |text: &str| {
            ResponseTemplate::new(200).set_body_json(json!({"response": {"status": text}}))
        };

        let server = answering(status("ERROR: Daily Limit Reached.")).await;
        assert_eq!(at(&server).query("q").await, Err(Failure::QuotaExhausted));

        let server = answering(status("ERROR: Something else.")).await;
        assert_eq!(
            at(&server).query("q").await,
            Err(Failure::Other("ERROR: Something else.".into()))
        );
    }

    /// The key is in the URL, which a transport error would otherwise print.
    #[tokio::test]
    async fn a_failed_request_does_not_print_the_key() {
        let free = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/s", free.local_addr().unwrap());
        drop(free);

        let failure = Mojeek::at(&url, "the-key").query("q").await.unwrap_err();

        assert!(matches!(failure, Failure::Other(_)));
        assert!(!failure.to_string().contains("the-key"), "{failure}");
    }
}

mod wikipedia {
    use super::*;

    fn at(server: &MockServer) -> Wikipedia {
        Wikipedia::at(&format!("{}/s", server.uri()), web())
    }

    #[tokio::test]
    async fn results_are_read_from_the_request_the_documentation_gives() {
        let server = MockServer::start().await;
        let answer = json!({"pages": [
            {
                "key": "Foo_(bar)?",
                "title": "Foo (bar)?",
                "excerpt": "<span class=\"searchmatch\">Foo</span> &amp; more",
                "description": "a foo",
            },
            {"key": "Baz", "title": "Baz", "excerpt": "", "description": "a baz"},
            {"key": "Qux", "title": "Qux", "excerpt": "", "description": null},
        ]});
        Mock::given(method("GET"))
            .and(path("/s"))
            .and(header("User-Agent", web().user_agent().as_str()))
            .and(query_param("q", "the query"))
            .and(query_param("limit", "20"))
            .respond_with(ResponseTemplate::new(200).set_body_json(answer))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server).query("the query").await.unwrap();

        assert_eq!(
            rows,
            [
                row("https://en.wikipedia.org/wiki/Foo_(bar)%3F", "Foo (bar)?", "Foo & more"),
                row("https://en.wikipedia.org/wiki/Baz", "Baz", "a baz"),
                row("https://en.wikipedia.org/wiki/Qux", "Qux", ""),
            ]
        );
    }

    #[tokio::test]
    async fn a_rate_limit_rests_it() {
        let server = answering(ResponseTemplate::new(429)).await;

        assert_eq!(at(&server).query("q").await, Err(Failure::RateLimited { retry_after: None }));
    }

    /// With no key to replace, a refusal must not switch it off for good.
    #[tokio::test]
    async fn a_refusal_is_not_about_a_key() {
        let server = answering(ResponseTemplate::new(403)).await;

        let failure = at(&server).query("q").await.unwrap_err();

        assert!(matches!(failure, Failure::Other(_)), "{failure:?}");
    }
}

mod arxiv {
    use super::*;

    fn at(server: &MockServer) -> Arxiv {
        Arxiv::at(&format!("{}/s", server.uri()))
    }

    const FEED: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title type="html">ArXiv Query</title>
  <id>http://arxiv.org/api/abc</id>
  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">2</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>A long
      title</title>
    <summary>  Bounds on a &lt; b
      and more.
    </summary>
    <author><name>A. Author</name></author>
    <link href="http://arxiv.org/abs/2401.00001v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2401.00001v1" rel="related"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00002v2</id>
    <title>Short</title>
    <summary>Text.</summary>
  </entry>
</feed>"#;

    #[tokio::test]
    async fn results_are_read_from_the_request_the_documentation_gives() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/s"))
            .and(query_param("search_query", "all:the query"))
            .and(query_param("max_results", "20"))
            .respond_with(ResponseTemplate::new(200).set_body_string(FEED))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server).query("the query").await.unwrap();

        assert_eq!(
            rows,
            [
                row(
                    "https://arxiv.org/abs/2401.00001v1",
                    "A long title",
                    "Bounds on a < b and more."
                ),
                row("https://arxiv.org/abs/2401.00002v2", "Short", "Text."),
            ]
        );
    }

    #[tokio::test]
    async fn a_feed_with_no_entries_is_no_results() {
        let empty =
            r#"<feed xmlns="http://www.w3.org/2005/Atom"><title>ArXiv Query</title></feed>"#;
        let server = answering(ResponseTemplate::new(200).set_body_string(empty)).await;

        assert_eq!(at(&server).query("q").await, Ok(Vec::new()));
    }

    #[tokio::test]
    async fn an_error_comes_as_an_entry() {
        let feed = r#"<feed xmlns="http://www.w3.org/2005/Atom"><entry>
            <id>http://arxiv.org/api/errors#incorrect_id_format</id>
            <title>Error</title>
            <summary>incorrect id format</summary>
        </entry></feed>"#;
        let server = answering(ResponseTemplate::new(200).set_body_string(feed)).await;

        assert_eq!(at(&server).query("q").await, Err(Failure::Other("incorrect id format".into())));
    }
}
