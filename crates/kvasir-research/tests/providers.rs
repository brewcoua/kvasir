//! Each provider against a fake of its documented request and answer, including its errors.

use std::sync::Arc;
use std::time::Duration;

use kvasir_fetch::{Guard, Options, Web, system_lookup};
use kvasir_research::providers::{
    Arxiv, Bing, Brave, Crossref, DuckDuckGo, EuropePmc, Exa, Failure, Mojeek, OpenAlex, Provider,
    Row, SemanticScholar, Tavily, Wikipedia,
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

mod crossref {
    use super::*;

    #[tokio::test]
    async fn results_are_read_from_the_request_the_documentation_gives() {
        let server = MockServer::start().await;
        let answer = json!({"message": {"items": [
            {
                "URL": "https://doi.org/10.1/a",
                "title": ["The title"],
                "abstract": "<jats:p>An &amp; abstract</jats:p>",
                "publisher": "P",
            },
            {"URL": "https://doi.org/10.1/b"},
        ]}});
        Mock::given(method("GET"))
            .and(path("/s"))
            .and(query_param("query", "the query"))
            .and(query_param("rows", "20"))
            .and(query_param("mailto", "ops@example.org"))
            .respond_with(ResponseTemplate::new(200).set_body_json(answer))
            .expect(1)
            .mount(&server)
            .await;
        let crossref = Crossref::at(&format!("{}/s", server.uri()), Some("ops@example.org"));

        let rows = crossref.query("the query").await.unwrap();

        assert_eq!(
            rows,
            [
                row("https://doi.org/10.1/a", "The title", "An & abstract"),
                row("https://doi.org/10.1/b", "", ""),
            ]
        );
    }

    #[tokio::test]
    async fn a_rate_limit_rests_it() {
        let server = answering(ResponseTemplate::new(429).append_header("Retry-After", "7")).await;
        let crossref = Crossref::at(&format!("{}/s", server.uri()), None);

        assert_eq!(
            crossref.query("q").await,
            Err(Failure::RateLimited { retry_after: Some(Duration::from_secs(7)) })
        );
    }

    #[test]
    fn a_mailto_shortens_its_interval() {
        assert_eq!(Crossref::new(None).interval(), Duration::from_secs(1));
        assert_eq!(Crossref::new(Some("a@b.c")).interval(), Duration::from_millis(334));
    }
}

mod europe_pmc {
    use super::*;

    fn at(server: &MockServer) -> EuropePmc {
        EuropePmc::at(&format!("{}/s", server.uri()))
    }

    #[tokio::test]
    async fn results_are_read_from_the_request_the_documentation_gives() {
        let server = MockServer::start().await;
        let answer = json!({"resultList": {"result": [
            {
                "id": "1",
                "source": "MED",
                "doi": "10.1/a",
                "title": "A <i>title</i>.",
                "abstractText": "An <b>abstract</b>.",
            },
            {"id": "2", "source": "PPR", "title": "No DOI"},
            {"title": "No address"},
        ]}});
        Mock::given(method("GET"))
            .and(path("/s"))
            .and(query_param("format", "json"))
            .and(query_param("query", "the query"))
            .and(query_param("pageSize", "20"))
            .and(query_param("resultType", "core"))
            .respond_with(ResponseTemplate::new(200).set_body_json(answer))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server).query("the query").await.unwrap();

        assert_eq!(
            rows,
            [
                row("https://doi.org/10.1/a", "A title.", "An abstract."),
                row("https://europepmc.org/article/PPR/2", "No DOI", ""),
            ]
        );
    }

    #[tokio::test]
    async fn a_failure_is_named() {
        let server = answering(ResponseTemplate::new(500)).await;

        let failure = at(&server).query("q").await.unwrap_err();

        assert!(matches!(failure, Failure::Other(_)), "{failure:?}");
    }
}

mod semantic_scholar {
    use super::*;

    fn at(server: &MockServer, key: Option<&str>) -> SemanticScholar {
        SemanticScholar::at(&format!("{}/s", server.uri()), key)
    }

    #[tokio::test]
    async fn results_are_read_from_the_request_the_documentation_gives() {
        let server = MockServer::start().await;
        let answer = json!({"total": 3, "data": [
            {"paperId": "a", "url": "https://s2.org/a", "title": "A", "abstract": "About\n a."},
            {"paperId": "b", "url": "https://s2.org/b", "title": null, "abstract": null},
            {"paperId": "c", "url": null, "title": "No address"},
        ]});
        Mock::given(method("GET"))
            .and(path("/s"))
            .and(header("x-api-key", "the-key"))
            .and(query_param("query", "the query"))
            .and(query_param("limit", "20"))
            .and(query_param("fields", "title,abstract,url"))
            .respond_with(ResponseTemplate::new(200).set_body_json(answer))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server, Some("the-key")).query("the query").await.unwrap();

        assert_eq!(
            rows,
            [row("https://s2.org/a", "A", "About a."), row("https://s2.org/b", "", "")]
        );
    }

    #[tokio::test]
    async fn it_answers_without_a_key_and_with_no_data_when_nothing_matches() {
        let server = answering(ResponseTemplate::new(200).set_body_json(json!({"total": 0}))).await;

        assert_eq!(at(&server, None).query("q").await, Ok(Vec::new()));
        let requests = server.received_requests().await.unwrap();
        assert!(!requests[0].headers.contains_key("x-api-key"));
    }

    #[tokio::test]
    async fn a_refusal_is_about_the_key_only_when_there_is_one() {
        let server = answering(ResponseTemplate::new(403)).await;

        assert_eq!(at(&server, Some("k")).query("q").await, Err(Failure::Unauthorized));
        let failure = at(&server, None).query("q").await.unwrap_err();
        assert!(matches!(failure, Failure::Other(_)), "{failure:?}");
    }
}

mod openalex {
    use super::*;

    fn at(server: &MockServer) -> OpenAlex {
        OpenAlex::at(&format!("{}/s", server.uri()), "the-key")
    }

    #[tokio::test]
    async fn results_are_read_from_the_request_the_documentation_gives() {
        let server = MockServer::start().await;
        let answer = json!({"results": [
            {
                "id": "https://openalex.org/W1",
                "doi": "https://doi.org/10.1/a",
                "display_name": "A",
                "primary_location": {"landing_page_url": "https://pub.org/a"},
                "abstract_inverted_index": {"the": [0, 2], "word": [1], "again": [3]},
            },
            {
                "id": "https://openalex.org/W2",
                "doi": null,
                "display_name": "B",
                "primary_location": {"landing_page_url": "https://pub.org/b"},
                "abstract_inverted_index": null,
            },
            {"id": "https://openalex.org/W3", "primary_location": null},
            {"display_name": "No address"},
        ]});
        Mock::given(method("GET"))
            .and(path("/s"))
            .and(query_param("search", "the query"))
            .and(query_param("per-page", "20"))
            .and(query_param("api_key", "the-key"))
            .respond_with(ResponseTemplate::new(200).set_body_json(answer))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server).query("the query").await.unwrap();

        assert_eq!(
            rows,
            [
                row("https://doi.org/10.1/a", "A", "the word the again"),
                row("https://pub.org/b", "B", ""),
                row("https://openalex.org/W3", "", ""),
            ]
        );
    }

    #[tokio::test]
    async fn a_refused_key_and_a_rate_limit_are_told_apart() {
        let server = answering(ResponseTemplate::new(401)).await;
        assert_eq!(at(&server).query("q").await, Err(Failure::Unauthorized));

        let server = answering(ResponseTemplate::new(429)).await;
        assert_eq!(at(&server).query("q").await, Err(Failure::RateLimited { retry_after: None }));
    }

    /// The key is in the URL, which a transport error would otherwise print.
    #[tokio::test]
    async fn a_failed_request_does_not_print_the_key() {
        let free = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/s", free.local_addr().unwrap());
        drop(free);

        let failure = OpenAlex::at(&url, "the-key").query("q").await.unwrap_err();

        assert!(matches!(failure, Failure::Other(_)));
        assert!(!failure.to_string().contains("the-key"), "{failure}");
    }
}

mod duckduckgo {
    use wiremock::matchers::body_string_contains;

    use super::*;

    fn at(server: &MockServer) -> DuckDuckGo {
        DuckDuckGo::at(&format!("{}/s", server.uri()))
    }

    fn page(body: &str) -> ResponseTemplate {
        ResponseTemplate::new(200).set_body_string(format!("<html><body>{body}</body></html>"))
    }

    #[tokio::test]
    async fn results_are_read_from_the_page() {
        let server = MockServer::start().await;
        let body = r#"<div id="links">
            <div class="result results_links web-result result--ad">
              <h2><a href="https://duckduckgo.com/y.js?ad=1">An ad</a></h2>
            </div>
            <div class="result results_links web-result">
              <h2 class="result__title"><a class="result__a" href="https://a.org/x">The
                 <b>title</b></a></h2>
              <a class="result__snippet" href="https://a.org/x">About <b>a</b>.</a>
            </div>
            <div class="result web-result">
              <h2><a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fb.org%2Fy%3Fz%3D1&amp;rut=abc">B</a></h2>
            </div>
            <div class="result web-result"><h2><a>No address</a></h2></div>
          </div>"#;
        Mock::given(method("POST"))
            .and(path("/s"))
            .and(header("content-type", "application/x-www-form-urlencoded"))
            .and(body_string_contains("q=the+query"))
            .respond_with(page(body))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server).query("the query").await.unwrap();

        assert_eq!(
            rows,
            [row("https://a.org/x", "The title", "About a."), row("https://b.org/y?z=1", "B", "")]
        );
    }

    #[tokio::test]
    async fn no_match_is_no_results() {
        let server =
            answering(page(r#"<div id="links"><div class="no-results">No</div></div>"#)).await;

        assert_eq!(at(&server).query("q").await, Ok(Vec::new()));
    }

    #[tokio::test]
    async fn its_blocks_are_told_apart() {
        for body in
            [r#"<form id="challenge-form"></form>"#, r#"<div class="anomaly-modal__title"></div>"#]
        {
            let server = answering(page(body)).await;
            assert_eq!(at(&server).query("q").await, Err(Failure::Captcha));
        }

        let server = answering(ResponseTemplate::new(202)).await;
        assert_eq!(at(&server).query("q").await, Err(Failure::RateLimited { retry_after: None }));

        let server = answering(ResponseTemplate::new(303)).await;
        assert!(matches!(at(&server).query("q").await, Err(Failure::Other(_))));
    }

    /// An empty list here would look like a topic with no sources.
    #[tokio::test]
    async fn a_page_it_does_not_understand_is_a_failure() {
        let server = answering(page("<p>Something new</p>")).await;

        assert_eq!(
            at(&server).query("q").await,
            Err(Failure::Other("no result list in the page".into()))
        );
    }
}

mod bing {
    use super::*;

    fn at(server: &MockServer) -> Bing {
        Bing::at(&format!("{}/s", server.uri()))
    }

    #[tokio::test]
    async fn results_are_read_from_the_page() {
        let server = MockServer::start().await;
        // "aHR0cHM6Ly9iLm9yZy95" is https://b.org/y.
        let body = r#"<html><body><ol id="b_results">
            <li class="b_ad"><h2><a href="https://ad.example">An ad</a></h2></li>
            <li class="b_algo">
              <h2><a href="https://a.org/x">The <strong>title</strong></a></h2>
              <p><span class="algoSlug_icon">Web</span>About <strong>a</strong>.</p>
            </li>
            <li class="b_algo">
              <h2><a href="https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9iLm9yZy95&ntb=1">B</a></h2>
            </li>
            <li class="b_algo"><h2><a href="https://c.org/no-title"></a></h2></li>
          </ol></body></html>"#;
        Mock::given(method("GET"))
            .and(path("/s"))
            .and(query_param("q", "the query"))
            .and(query_param("setlang", "en"))
            .respond_with(ResponseTemplate::new(200).set_body_string(body))
            .expect(1)
            .mount(&server)
            .await;

        let rows = at(&server).query("the query").await.unwrap();

        assert_eq!(
            rows,
            [row("https://a.org/x", "The title", "About a."), row("https://b.org/y", "B", "")]
        );
    }

    #[tokio::test]
    async fn a_page_with_no_list_is_a_failure() {
        let server =
            answering(ResponseTemplate::new(200).set_body_string("<p>Verify you are human</p>"))
                .await;

        assert_eq!(
            at(&server).query("q").await,
            Err(Failure::Other("no result list in the page".into()))
        );
    }
}
