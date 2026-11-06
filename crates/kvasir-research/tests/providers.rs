//! Each provider against a fake of its documented request and answer, including its errors.

use std::time::Duration;

use kvasir_research::providers::{Brave, Failure, Provider, Row, Tavily};
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
