import json
from pathlib import Path

import pytest

from kvasir.outputs import OutputError, read_article, read_citations, read_outline

FIXTURES = Path(__file__).parent / "fixtures" / "storm_output"
POLISHED = FIXTURES / "polished"
DRAFT_ONLY = FIXTURES / "draft_only"


def test_prefers_the_polished_article():
    article = read_article(POLISHED)

    assert article.startswith("# summary")
    assert "# Reception" in article


def test_falls_back_to_the_draft_when_polishing_was_disabled():
    article = read_article(DRAFT_ONLY)

    assert article.startswith("# Discovery")


def test_falls_back_when_the_polished_file_exists_but_is_empty(tmp_path):
    (tmp_path / "storm_gen_article_polished.txt").write_text("   \n")
    (tmp_path / "storm_gen_article.txt").write_text("# Discovery\n")

    assert read_article(tmp_path) == "# Discovery"


def test_missing_article_is_an_error(tmp_path):
    with pytest.raises(OutputError, match="no article"):
        read_article(tmp_path)


def test_outline_is_read():
    assert read_outline(POLISHED) == "# Discovery\n## Early accounts\n# Reception"


def test_missing_outline_is_not_an_error():
    assert read_outline(DRAFT_ONLY) == ""


def test_citations_are_numbered_and_ordered():
    citations = read_citations(POLISHED)

    assert [citation.index for citation in citations] == [1, 2, 3]
    assert citations[0].url == "https://example.org/parish-survey"
    assert citations[0].title == "Parish drainage survey, 1783"


def test_citation_numbers_come_from_the_index_map_not_citation_uuid():
    # Every source in the fixture carries citation_uuid -1, as upstream writes it. Reading that
    # field instead of url_to_unified_index would number every citation -1.
    raw = json.loads((POLISHED / "url_to_info.json").read_text())
    assert {info["citation_uuid"] for info in raw["url_to_info"].values()} == {-1}

    assert [citation.index for citation in read_citations(POLISHED)] == [1, 2, 3]


def test_citation_snippet_prefers_a_snippet_and_falls_back_to_the_description():
    by_url = {citation.url: citation for citation in read_citations(POLISHED)}

    assert by_url["https://example.org/parish-survey"].snippet.startswith("The stone appears")
    # This source has an empty snippets list, so the description stands in.
    assert by_url["https://example.org/field-notes"].snippet == "Unpublished field notes."


def test_ordering_survives_an_unordered_index_map(tmp_path):
    (tmp_path / "url_to_info.json").write_text(
        json.dumps(
            {
                "url_to_unified_index": {"https://b.example": 2, "https://a.example": 1},
                "url_to_info": {},
            }
        )
    )

    assert [citation.index for citation in read_citations(tmp_path)] == [1, 2]


def test_missing_references_yield_no_citations(tmp_path):
    assert read_citations(tmp_path) == []


def test_unreadable_references_are_an_error(tmp_path):
    (tmp_path / "url_to_info.json").write_text("{not json")

    with pytest.raises(OutputError, match="cannot read"):
        read_citations(tmp_path)


def test_fixture_matches_what_upstream_actually_writes(tmp_path):
    """Guard against the fixture drifting from upstream's serialiser.

    Loading the fixture with StormArticle.from_string and dumping it again with upstream's own
    dump_reference_to_file must reproduce the fixture, or the fixture is not a real STORM output.
    """
    from knowledge_storm.storm_wiki.modules.storm_dataclass import StormArticle

    expected = json.loads((POLISHED / "url_to_info.json").read_text())

    article = StormArticle.from_string(
        topic_name="The Kvasir stone",
        article_text=(POLISHED / "storm_gen_article_polished.txt").read_text(),
        references=json.loads((POLISHED / "url_to_info.json").read_text()),
    )
    article.dump_reference_to_file(str(tmp_path / "url_to_info.json"))

    assert json.loads((tmp_path / "url_to_info.json").read_text()) == expected
