# STORM

The `storm` method is Stanford OVAL's [STORM](https://github.com/stanford-oval/storm), translated
into Rust in `crates/kvasir-storm`. It was translated from the `knowledge_storm` package at
[`fb951af`](https://github.com/stanford-oval/storm/commit/fb951af7744dab086e34962e9bc6fe878e145f83)
(2025-09-30), by way of the Python fork kvasir ran before the Rust service. The algorithm and the
prompts are upstream's. The crate is MIT, as upstream is, and carries upstream's `LICENSE` and a
`NOTICE`; the rest of kvasir is MIT OR Apache-2.0.

Read this before changing anything in `crates/kvasir-storm`. It records what the method must keep
doing, and each place it differs from upstream, so that reading upstream against the crate does not
start from scratch. Upstream is dormant: its last substantive change was in January 2025.

## What was translated

Plain STORM, the `storm_wiki` pipeline, in four stages:

| Stage | What happens | Module |
| --- | --- | --- |
| `research` | Personas are drawn from related topics, and each editor holds a conversation with an expert who searches SearXNG to answer. | `curation.rs` |
| `outline` | A first outline from the topic alone, then one refined by the conversations. | `write.rs` |
| `article` | Each top-level section written from the snippets closest to its queries. | `write.rs` |
| `polish` | A lead section written for the whole article. Skipped with `do_polish_article` off. | `write.rs` |

`article.rs` holds the article as a tree of sections and numbers its citations; `text.rs` holds the
text handling upstream did with regexes; `signatures.rs` holds the ten prompts.

Not translated: Co-STORM, which kvasir no longer offers; every retriever but SearXNG; the output
directory and its files, since a run returns its article instead; and `PolishPage`, which ran only
with `remove_duplicate`, a flag nothing set.

STORM reads search snippets only, never the pages, so a run fetches no page except the related
topics' tables of contents during `research`.

## What it must keep doing

Each of these was a bug in upstream that produced a bad article rather than an error, or a guard
upstream had that is easy to lose. `crates/kvasir-storm/tests/storm.rs` pins each one, by the test
named.

- **A failed query keeps its siblings.** Upstream searched an expert's queries with a map that
  discarded every result when one query failed. One failure now costs only that query
  (`one_failing_query_does_not_discard_its_siblings`).
- **A failed answer ends the conversation with what it has.** Upstream put a canned apology into the
  conversation as if the expert had said it. A failed turn now ends that editor's conversation, and
  the turns before it are kept (`a_failed_answer_ends_the_conversation_with_what_it_has`).
- **An expert with nothing found says so.** Upstream's guard against a made-up answer is kept: when
  no search returns anything, the expert answers `NOTHING_FOUND` rather than asking the model
  (`an_expert_that_found_nothing_says_so_rather_than_answering`).
- **Nothing collected means nothing retrieved.** Upstream failed on an empty table of snippets. A
  section query against it now retrieves nothing, and the section is written without sources
  (`a_section_query_against_nothing_collected_retrieves_nothing`), so a run that finds nothing
  still writes an article (`a_run_that_finds_nothing_still_writes_an_article`).
- **Searches stay within `KVASIR_MAX_THREADS`, and a query repeated within one retrieval is
  searched once** (`a_query_is_searched_once_and_the_searches_stay_within_the_width`).
- **A site to avoid is dropped.** With `sources.enabled`, a result whose site resolves to tier
  `avoid` is never read (`a_result_from_a_site_to_avoid_is_dropped`).
- **A URL the model chose must be public.** Upstream fetched whatever related-topic pages the model
  named and followed redirects, so a page that steered the model could point it at the service's
  own network. Each hop now goes through the same public-address check as the rest of kvasir, with
  redirects followed by hand and at most 5 of them (`a_url_the_model_chose_must_be_public`,
  `a_redirect_to_a_private_address_is_not_followed`, `redirects_stop_at_a_limit`).
- **A credential never shows.** Upstream wrote each model's settings, key included, to
  `run_config.json`. Nothing is written now, and a model target's `Debug` leaves out its key
  (`a_target_shows_no_credential`).
- **The lead goes first** (`a_run_writes_a_cited_article_with_its_lead_first`).

The SearXNG client in `crates/kvasir-research` follows the 308 that current SearXNG answers its
base URL with. Upstream did not, so every query read an HTML page and the run found nothing.
`crates/kvasir-research/tests/search.rs` pins it.

Every prompt is pinned byte for byte by `tests/golden/signatures.json`, through
`crates/kvasir-storm/tests/golden.rs`.

## Where it differs from upstream

The prompts are upstream's as dspy 3.3.0 renders them, which differs from what upstream sent on dspy
2.4.9: each field is marked `[[ ## name ## ]]`, and an output with structure, such as a list of
personas or of queries, states its type and is parsed as that type. Where upstream picked structure
out of prose with regexes, a reply that does not parse is asked for once more as JSON, as dspy
does, and then fails the call, rather than reading as `"Undefined"`.

Deliberately:

- Citations are numbered from the article returned. Upstream's output files numbered them from the
  draft, which no longer matched once polishing rewrote the article.
- An outline heading at the topic's own level goes under the topic. Upstream emptied its stack and
  failed.
- Conversations and sections are collected in persona and outline order, not in the order they
  finish, so a run's article does not depend on timing.
- A source's snippets keep the order they were found in. Upstream kept them in a `set`.
- A section's queries are embedded in one call, and every embedding uses `model.embedding` through
  its provider. Upstream loaded a local `SentenceTransformer` for this.
- A model call is retried only on what a retry can fix: a rate limit, a server error, a timeout or
  a dropped connection. A bad request or a refused key fails at once.
- The request for a related page's contents sends the service's user agent.

Kept from upstream, though they look wrong:

- `search_top_k` changes nothing. Upstream's SearXNG retriever and its expert never read it, so the
  option is accepted and ignored.
- When a section of `n` sources cites a number above `n`, every citation numbered `n` or more is
  removed, so the citation of the last source goes too (`without_citations_from` in `text.rs`).
- An outline whose first heading is not the topic, spelled as asked, keeps that heading as a section
  of its own. With a model that writes "Rök Runestone" for the topic "The Rök runestone", the whole
  outline then sits under one section.

## Taking a later upstream

Diff `fb951af` against the new revision and port what it touches by hand. A change is either a fix
upstream made independently, in which case take theirs and drop the entry above, or one to keep, in
which case reapply it. A changed prompt changes `tests/golden/signatures.json` with it. Check with:

```bash
cargo nextest run --locked -p kvasir-storm -p kvasir-research
```
