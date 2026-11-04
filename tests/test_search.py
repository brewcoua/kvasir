"""Choosing which results to read: SearXNG's score fused with closeness to the question."""

from __future__ import annotations

import logging

import numpy as np

from fakes import Search, Vectors
from kvasir.research.search import MAX_QUERY_CHARACTERS, search

QUESTION = "when was the stone found"


def hit(name, score, domain=None, text=""):
    return {
        "url": f"https://{domain or name}.org/{name}",
        "title": name,
        "description": text,
        "snippets": [text],
        "score": score,
    }


def urls(hits):
    return [h["url"].split("/")[-1] for h in hits]


def test_a_low_scored_hit_that_matches_the_question_beats_a_high_scored_one_that_does_not():
    hits = [hit("far", 3.0), hit("near", 2.0), hit("other", 1.0)]
    encoder = Vectors(
        {QUESTION: [1, 0], "far\n": [0, 1], "near\n": [1, 0], "other\n": [1, 1]},
    )

    picked = search(Search(hits), ["q"], 1, set(), encoder, QUESTION)

    assert urls(picked) == ["near"]


def test_hits_are_taken_by_score_not_by_response_order():
    hits = [hit("low", 1.0), hit("high", 5.0), hit("mid", 3.0)]

    picked = search(Search(hits), ["q"], 2, set())

    assert urls(picked) == ["high", "mid"]


def test_a_near_duplicate_snippet_and_a_repeated_domain_are_skipped():
    hits = [
        hit("a", 4.0),
        hit("copy", 3.0),
        hit("same-site", 2.0, domain="a"),
        hit("other", 1.0),
    ]
    encoder = Vectors(
        {
            QUESTION: [1, 0],
            "a\n": [1, 0],
            "copy\n": [1, 0.1],
            "same-site\n": [0, 1],
            "other\n": [0.5, 1],
        }
    )
    seen: set[str] = set()

    picked = search(Search(hits), ["q"], 3, seen, encoder, QUESTION)

    assert urls(picked) == ["a", "other"]
    assert seen == {h["url"] for h in picked}


def test_urls_already_seen_are_still_excluded():
    hits = [hit("a", 2.0), hit("b", 1.0)]
    seen = {"https://a.org/a"}

    picked = search(Search(hits), ["q"], 2, seen, Vectors({}, default=[1, 0]), QUESTION)

    assert urls(picked) == ["b"]


def test_without_an_encoder_the_score_order_and_the_domain_skip_apply():
    hits = [hit("x", 1.0), hit("y", 3.0, domain="x"), hit("z", 2.0)]

    picked = search(Search(hits), ["q"], 3, set())

    assert urls(picked) == ["y", "z"]


def test_what_the_run_holds_lowers_a_hit_it_repeats():
    hits = [hit("old", 2.0), hit("new", 1.0)]
    encoder = Vectors({QUESTION: [1, 1], "old\n": [1, 0], "new\n": [0.1, 1]})
    held = np.array([[1.0, 0.0]])

    picked = search(Search(hits), ["q"], 1, set(), encoder, QUESTION, held)

    assert urls(picked) == ["new"]


class Recording(Search):
    def __init__(self):
        super().__init__([])
        self.queries = []

    def __call__(self, query, exclude_urls):
        self.queries.append(query)
        return super().__call__(query, exclude_urls)


def test_a_degenerate_query_reaches_searxng_capped_with_balanced_quotes(caplog):
    terms = " ".join(f'"Linux 6.{n}"' for n in range(12, 400))
    query = f'{terms} "Linux 6.4'
    searxng = Recording()

    with caplog.at_level(logging.INFO, logger="kvasir.research.search"):
        search(searxng, [query], 3, set())

    (sent,) = searxng.queries
    assert len(query) > 10 * MAX_QUERY_CHARACTERS
    assert len(sent) <= MAX_QUERY_CHARACTERS
    assert sent.count('"') % 2 == 0
    assert sent.endswith('"')
    assert query.startswith(sent)
    assert "cut from" in caplog.text


def test_a_cut_leaves_no_dangling_operator():
    searxng = Recording()
    query = "word " * 59 + "OR -" + "x" * 50

    search(searxng, [query], 3, set())

    assert searxng.queries == [("word " * 59).rstrip()]


def test_a_short_query_is_sent_as_written():
    searxng = Recording()

    search(searxng, ['"a b" OR c'], 3, set())

    assert searxng.queries == ['"a b" OR c']
