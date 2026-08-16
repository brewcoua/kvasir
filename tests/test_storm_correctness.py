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


def test_an_expert_description_may_contain_a_colon():
    """Upstream unpacked split(":") into two names, so a second colon raised ValueError."""
    from types import SimpleNamespace

    from kvasir.storm.collaborative_storm.engine import DiscourseManager, RunnerArgument
    from kvasir.storm.logging_wrapper import LoggingWrapper

    lm_config = SimpleNamespace(
        discourse_manage_lm=None,
        utterance_polishing_lm=None,
        question_answering_lm=None,
        warmstart_outline_gen_lm=None,
        question_asking_lm=None,
        knowledge_base_lm=None,
        collect_and_reset_lm_usage=lambda: {},
        collect_and_reset_lm_history=lambda: [],
    )
    runner = SimpleNamespace(
        runner_argument=RunnerArgument(topic="the Antikythera mechanism"),
        lm_config=lm_config,
        logging_wrapper=LoggingWrapper(lm_config),
        rm=object(),
        callback_handler=None,
    )

    (expert,) = DiscourseManager._parse_expert_names_to_agent(
        runner, "Historian: studies the era: 1900-1950"
    )

    assert expert.role_name == "Historian"
    assert expert.role_description == "studies the era: 1900-1950"


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
