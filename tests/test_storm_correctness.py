"""Upstream defects that produced wrong results rather than errors."""

from __future__ import annotations

import json

import pytest

from kvasir.storm.utils import FileIOHelper


def test_a_string_survives_a_round_trip(tmp_path):
    """Upstream joined readlines() with "\\n", doubling every newline it read back."""
    path = tmp_path / "outline.txt"
    text = "# Topic\n## Section\ncontent\n"

    FileIOHelper.write_str(text, path)

    assert FileIOHelper.load_str(path) == text


def test_unserialisable_contents_raise_rather_than_being_substituted(tmp_path):
    """Upstream wrote the string "non-serializable contents" in their place."""
    path = tmp_path / "url_to_info.json"

    with pytest.raises(TypeError):
        FileIOHelper.dump_json({"info": object()}, path)

    assert not path.exists()


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path):
    path = tmp_path / "url_to_info.json"
    FileIOHelper.dump_json({"url_to_unified_index": {}}, path)

    with pytest.raises(TypeError):
        FileIOHelper.dump_json({"info": object()}, path)

    assert json.loads(path.read_text()) == {"url_to_unified_index": {}}
    assert list(tmp_path.iterdir()) == [path]


def test_one_failing_query_does_not_discard_its_siblings():
    """Upstream used executor.map, which re-raises on the first failure and loses the rest."""
    from kvasir.storm.interface import Retriever

    class _RM:
        def __call__(self, query_or_queries, exclude_urls):
            (query,) = query_or_queries
            if query == "bad":
                raise RuntimeError("search backend is down")
            return [
                {
                    "url": f"https://example.invalid/{query}",
                    "title": query,
                    "description": "",
                    "snippets": [f"about {query}"],
                }
            ]

    retriever = Retriever(rm=_RM(), max_thread=4)
    results = retriever.retrieve(["good", "bad", "also good"])

    assert sorted(info.title for info in results) == ["also good", "good"]


def test_the_run_configuration_carries_no_credential():
    """Upstream returned kwargs whole, so run_config.json held the gateway key in plaintext."""
    from kvasir.storm.lm import GatewayModel
    from kvasir.storm.storm_wiki.engine import STORMWikiLMConfigs

    configs = STORMWikiLMConfigs()
    configs.set_article_gen_lm(
        GatewayModel(
            model="openai/ollama/strong:cloud",
            api_key="a-real-key",
            api_base="https://gateway.example/v1",
            max_tokens=700,
        )
    )

    logged = configs.log()

    assert logged["article_gen_lm"]["max_tokens"] == 700
    assert "a-real-key" not in json.dumps(logged)
    assert not [key for key in logged["article_gen_lm"] if key.startswith("api_")]


def test_a_finished_run_survives_an_unwritable_record(tmp_path):
    """post_run writes a record of the run, minutes after the article itself is already done."""
    from types import SimpleNamespace

    from kvasir.storm.storm_wiki.engine import STORMWikiRunner

    class _Configs:
        def log(self):
            return {"article_gen_lm": {"callback": object()}}

        def collect_and_reset_lm_history(self):
            return []

    runner = SimpleNamespace(lm_configs=_Configs(), article_output_dir=str(tmp_path))

    STORMWikiRunner.post_run(runner)

    assert not (tmp_path / "run_config.json").exists()
    # The second artefact is written even though the first could not be.
    assert (tmp_path / "llm_call_history.jsonl").exists()


def test_searxng_follows_its_redirect_to_search(monkeypatch):
    """SearXNG answers its base URL with a 308 to /search, which httpx does not follow by default,
    so every query came back as an HTML page and the run collected nothing."""
    from kvasir.storm.rm import SearXNG

    sent = {}

    class _Response:
        def json(self):
            return {"results": [{"url": "https://example.invalid/a", "content": "about a"}]}

    def get(url, **kwargs):
        sent.update(kwargs)
        return _Response()

    monkeypatch.setattr("kvasir.storm.rm.httpx.get", get)
    results = SearXNG(searxng_api_url="http://searxng.invalid")("a")

    assert sent["follow_redirects"] is True
    assert [result["url"] for result in results] == ["https://example.invalid/a"]


def test_searxng_passes_on_the_score_of_a_result(monkeypatch):
    from kvasir.storm.rm import SearXNG

    class _Response:
        def json(self):
            return {
                "results": [
                    {"url": "https://example.invalid/a", "score": 2.5},
                    {"url": "https://example.invalid/b"},
                ]
            }

    monkeypatch.setattr("kvasir.storm.rm.httpx.get", lambda url, **kwargs: _Response())
    results = SearXNG(searxng_api_url="http://searxng.invalid")("a")

    assert [r["score"] for r in results] == [2.5, 0.0]


def test_searxng_says_why_a_query_came_back_empty(monkeypatch):
    """A rate-limited engine answers with no results, which reads as a topic nothing covers."""
    from kvasir.storm.rm import SearXNG

    class _Response:
        def json(self):
            return {
                "results": [],
                "unresponsive_engines": [["brave", "Suspended: too many requests"]],
            }

    answers = iter([_Response(), RuntimeError("connection refused")])

    def get(url, **kwargs):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr("kvasir.storm.rm.httpx.get", get)
    search = SearXNG(searxng_api_url="http://searxng.invalid")

    assert search(["a", "b"]) == []
    assert search.failures == [
        "brave: Suspended: too many requests",
        "RuntimeError: connection refused",
    ]


def test_a_section_query_against_nothing_collected_retrieves_nothing():
    """A research stage that found no source left an empty table, whose (0, 0) embedding failed
    the similarity product and with it the whole run."""
    import numpy as np

    from kvasir.storm.storm_wiki.modules.storm_dataclass import StormInformationTable

    class _Encoder:
        def encode(self, texts, query=False):
            return np.ones(768) if isinstance(texts, str) else np.empty((0, 0))

    table = StormInformationTable([])
    table.prepare_table_for_retrieval(_Encoder())

    assert table.retrieve_information("History", search_top_k=3) == []


PUBLIC = "http://93.184.216.34"
METADATA = "http://169.254.169.254/latest/meta-data"


def _wiki_answers(monkeypatch, answers):
    """Stands in for the network, and records what was asked of it."""
    import httpx

    from kvasir.storm.storm_wiki.modules import persona_generator

    asked = []

    def get(url, **kwargs):
        asked.append(url)
        assert not kwargs.get("follow_redirects"), "a hop must not skip the check"
        status, headers = answers[url]
        return httpx.Response(status, headers=headers, text="<h1>Title</h1><h2>Section</h2>")

    monkeypatch.setattr(persona_generator.httpx, "get", get)
    return persona_generator.get_wiki_page_title_and_toc, asked


@pytest.mark.parametrize(
    "url",
    [METADATA, "http://127.0.0.1:8080/v1/settings", "http://[::1]/", "file:///etc/passwd"],
)
def test_a_url_the_model_chose_must_be_public(monkeypatch, url):
    read, asked = _wiki_answers(monkeypatch, {})

    with pytest.raises(ValueError, match="not a public URL"):
        read(url)

    assert asked == []


def test_a_redirect_to_a_private_address_is_not_followed(monkeypatch):
    read, asked = _wiki_answers(monkeypatch, {f"{PUBLIC}/a": (302, {"location": METADATA})})

    with pytest.raises(ValueError, match="not a public URL"):
        read(f"{PUBLIC}/a")

    assert asked == [f"{PUBLIC}/a"]


def test_a_redirect_between_public_pages_is_followed(monkeypatch):
    read, asked = _wiki_answers(
        monkeypatch,
        {f"{PUBLIC}/wiki/a": (301, {"location": "/wiki/b"}), f"{PUBLIC}/wiki/b": (200, {})},
    )

    assert read(f"{PUBLIC}/wiki/a") == ("Title", "Section")
    assert asked == [f"{PUBLIC}/wiki/a", f"{PUBLIC}/wiki/b"]


def test_redirects_stop_at_a_limit(monkeypatch):
    read, asked = _wiki_answers(monkeypatch, {f"{PUBLIC}/a": (302, {"location": "/a"})})

    with pytest.raises(ValueError, match="too many redirects"):
        read(f"{PUBLIC}/a")

    assert len(asked) == 6
