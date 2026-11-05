# Source trust: plan

Status: written 2026-11-04. Phases 1 to 5 built 2026-11-05; Co-STORM citations (`conversation.py`) carry no source.

kvasir treats every site alike. A ministry, a newspaper and a content farm each count as one
source, and the only sign of quality is site affinity: a site's corroborated claims over its pages
read. This plan gives each site a kind (government, news, reference and so on), tags such as
`community` or `ai_generated`, and a trust tier derived from both. The tier is shown next to each
citation, and the search pick, lead worth, site searches and corroboration use it. A site of tier
`avoid` is never read.

The feature is optional and off by default: `[sources] enabled = false`. Off, no site is classified,
no list is downloaded, Wikidata is never asked, and runs behave as after phase 1, which is a bug fix
and always applies.

## The problem, from a run

A run on WebAssembly adoption made 26 searches. 11 were `site:` searches, of `reintech.io`,
`asopi.tech`, `myhospitalnow.com`, `eduresearchjournal.com`, `wisedesk.in`, `ttlab.hopto.org` and
`ibuidl.org`. Many citations came from those sites, and some figures were doubtful ("67 % of new
enterprise projects include at least one Wasm module").

The cause is in `research/corpus.py` and `methods/ledger/frontier.py`:

1. `Frontier.merge` records a repeat as corroboration when the two claims come from different
   `domain()` strings. Content farms copy each other, so they corroborate each other.
2. `Corpus._affinity` divides those corroborations by the pages read of the site. A farm read
   twice with two copied claims reaches 1.0.
3. `Corpus.site_query` gives any site at `SITE_AFFINITY` (1.0) a `site:` search on every question
   searched once already, with no cap per run.

Nothing in the pipeline knows what a site is, so nothing stops the loop.

## Design rules, and the evidence behind them

Confidence follows [`deep-research-plan.md`](deep-research-plan.md): "high" is a primary source
read or a query run this session, "medium" a primary source read through a summary, "low" memory
or a fragment.

| Rule | Evidence | Confidence |
| --- | --- | --- |
| Classify by registrable domain and host, never by URL path. | `tld.get_fld` (already installed through courlan) returns `education.gouv.fr`, `wikipedia.org`, `europa.eu` and `foo.github.io` for their URLs: it reads the private section of the Public Suffix List. Run 2026-11-04. | high |
| Suffix rules come before Wikidata for kind. | `wdt:P13337 "europa.eu"` returns the Health-EU portal (Q684245), typed only "website". A sub-entity claimed the whole domain. Run 2026-11-04. | high |
| Look up Wikidata by `P13337` (domain name), then by `P856` (official website) with URL variants. | Of 13 sample domains, `P13337` matched lemonde.fr, nytimes.com, wikipedia.org, reuters.com, cdc.gov, mit.edu, grokipedia.com and europa.eu. `P856` with exact URLs matched commission.europa.eu, education.gouv.fr and en.wikipedia.org. Neither matched reintech.io or deepwiki.com. Run 2026-11-04. [P13337](https://www.wikidata.org/wiki/Property:P13337) | high |
| Classify a Wikidata item by `P31/P279*` against a list of root classes, taking the first by priority. | The roots below were checked on the sample items. CDC and MIT also reach Q4830453 "business", so priority must put government and academic first. Run 2026-11-04. | high |
| One query per batch does the lookup and the classification. | `P13337` and `P856` as two `UNION` branches, joined to `P31/wdt:P279*` over the roots, answered 12 hosts in 2.7 s. Run 2026-11-04. | high |
| Standards bodies have one root. | W3C, IETF, ISO, IEC, ETSI, ITU, IEEE, AFNOR, NIST, WHATWG, Ecma, OASIS, Khronos and Unicode all reach Q1328899 "standards organization"; Q15277759 "national standards organisation" is a subclass of it. Run 2026-11-04. | high |
| Intergovernmental is "international organization, not an NGO". | Q484652 also takes in NGOs: ISO reaches it through Q1194093 "international non-governmental organization". Q245065 "intergovernmental organization" misses WHO, WTO, ITU, IMF and the World Bank. Q484652 without Q79913 "non-governmental organization" keeps all 12 IGOs tried and drops ISO. Run 2026-11-04. | high |
| Wikidata marks some AI sites itself. | grokipedia.com (Q136410803) is `P31` Q125361038 "AI-generated content". Run 2026-11-04. | high |
| Most content farms have no Wikidata item; "unknown" is the common case and must not count as trusted. | None of the 7 farms of the run above is in the sample results. Inference from 2 of them checked. | medium |
| Batch queries, with a descriptive User-Agent, and stay within limits. | WDQS: 60 s timeout per query, 60 s of processing a minute per client, 5 parallel queries per IP, 429 with `Retry-After`. [User manual](https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual) | medium |
| Use the main WDQS graph; QLever is an alternative endpoint. | Since 9 May 2025 scholarly articles live in a separate graph; organisations stay in the main one. [Graph split](https://www.wikidata.org/wiki/Wikidata:SPARQL_query_service/WDQS_graph_split). QLever serves the full graph: [QLever wiki](https://github.com/ad-freiburg/qlever/wiki/Differences-between-QLever's-Wikidata-SPARQL-endpoint-and-the-Wikidata-Query-Service-(WDQS)) | medium |
| Use CISA's `.gov` list for US government domains below the suffix rules' reach. | [cisagov/dotgov-data](https://github.com/cisagov/dotgov-data), CC0, `current-full.csv`, updated daily. | medium |
| A community AI blocklist can be added, refreshed at runtime. | [laylavish/uBlockOrigin-HUGE-AI-Blocklist](https://github.com/laylavish/uBlockOrigin-HUGE-AI-Blocklist), about 1,000 domains, uBlacklist and hosts formats. CC0-1.0, but last commit 2025-10-02, and it lists sites with AI imagery, not content farms: left out of the default `lists`. Checked 2026-11-04. | medium |
| Wikipedia's perennial sources list is a reliability overlay, not a classifier. | Statuses: generally reliable, no consensus, generally unreliable, deprecated, blacklisted. No official machine-readable export; the page's wikitext is parsed. [WP:RSP](https://en.wikipedia.org/wiki/Wikipedia:Reliable_sources/Perennial_sources) | medium |
| Govdirectory adds no domain index. | It builds a directory of agencies from `P31`, `P17` and social-account properties, not `P856`. [WikiProject Govdirectory](https://www.wikidata.org/wiki/Wikidata:WikiProject_Govdirectory) | medium |

Deferred until there is an evaluation to prove them: the news quality scores of
[Lin et al. 2023](https://github.com/hauselin/domain-quality-ratings) (11,520 domains, license not
verified), Tranco rank as a weak prior, RDAP domain age, and ROR for universities outside `P31`.
Not used: MBFC and NewsGuard (no open license), UT1 and Curlie (stale or the wrong categories).

## What a site is

Each host resolves to one `Source`:

```python
@dataclass(frozen=True)
class Source:
    host: str  # the host asked about, normalized
    kind: str  # see the table below
    tags: frozenset[str]  # see the list below, plus allow or deny from the config or a mark
    label: str  # "French Ministry of education", or ""
    qid: str  # "Q2572330", or ""
    origin: str  # config, mark, list, rule, wikidata

    @property
    def tier(self) -> str: ...  # high, normal, unknown, low, avoid: derived, never stored
```

Kinds, the Wikidata roots that give them (QIDs checked 2026-11-04), and their priority:

| Priority | Kind | Suffix rule | Wikidata roots |
| --- | --- | --- | --- |
| 1 | `intergovernmental` | `.int`, `europa.eu` | Q484652 international organization, unless it also reaches Q79913 non-governmental organization |
| 2 | `government` | `.gov`, `.mil`, `.gov.<cc>`, `.gouv.<cc>`, `.gc.ca`, CISA list | Q327333 government agency, Q2659904 government organization, Q6256 country (`canada.ca` is the item of Canada) |
| 3 | `academic` | `.edu`, `.ac.<cc>`, `.edu.<cc>` | Q3918 university, Q31855 research institute, Q4671277 academic institution |
| 4 | `standards` | none | Q1328899 standards organization |
| 5 | `news` | none | Q11032 newspaper, Q192283 news agency, Q1153191 online newspaper |
| 6 | `reference` | none | Q5292 encyclopedia |
| 7 | `organization` | none | Q43229 organization (company, NGO, publisher) |
| 8 | `unknown` | none | none matched |

Tags are orthogonal to kind:

- `official`: the item's `P856` or `P13337` names this host. A ministry's site is official; so is
  a company's own product page.
- `community`: the item reaches Q171 wiki. Wikipedia is `reference` with `community`. Being
  Wikipedia's own site makes it `official` too, which says nothing about the facts it holds, so
  `official` raises the tier only for the government, intergovernmental and academic kinds.
- `ai_generated`: the item is `P31` Q125361038, or the host is on the bundled list or the AI
  blocklist. The bundled list starts with `deepwiki.com` and `grokipedia.com`.
- `content_farm`, `predatory`: the bundled list.
- `rsp_*`: WP:RSP status, when the phase 5 overlay is built.

The tier is computed in one function from kind and tags, first match wins:

| Tier | When |
| --- | --- |
| `avoid` | `ai_generated`, `content_farm`, `predatory` or `rsp_deprecated`; a `deny` |
| `low` | `rsp_unreliable` |
| `high` | `government`, `intergovernmental`, `academic` or `standards`; `rsp_reliable`; an `allow` |
| `normal` | any other known kind |
| `unknown` | `unknown` |

Wikipedia comes out `normal`. Its pages are dense, which search already rewards through relevance,
so it needs no tier of its own; if the evaluation shows it should be read first for background,
that is a bonus on the `reference` kind in lead worth, not a higher tier.

## How a host resolves

```text
host -> config allow/deny -> mark -> bundled list -> downloaded lists -> suffix rules -> cache -> Wikidata -> unknown
```

1. Lowercase, IDNA, drop `www.` and the port: `favicons.normalize_host` does this already.
2. `allow` and `deny` in `[sources]` match the host or any parent. They win over everything, and
   the Knowledge page shows them as set in the config, not editable.
3. A mark is set from the Knowledge page: `allow`, `deny`, or a kind. It is kept in the `marks`
   table of `sources.db` with no expiry, until the user clears it.
4. The bundled list `research/sources.toml` and the downloaded lists match the host or its
   registrable domain.
5. Suffix rules set the kind. Wikidata still runs for a label and `official`, but cannot change a
   kind a rule set: this is the `europa.eu` case.
6. The cache, `data_dir/sources.db`, holds Wikidata answers and misses with `fetched_at`, kept
   `ttl_days` (30) and `miss_ttl_days` (7). It follows `FaviconStore`: one table, WAL, one
   connection per operation.
7. Wikidata misses are batched: up to 50 hosts a query. For each host, try the host, then its
   registrable domain (`en.wikipedia.org`, then `wikipedia.org`). One query does both lookups and
   the classification: `P13337` by name, `P856` over 8 URL variants a name (http and https, with
   and without `www.`, with and without a trailing slash), and every matched item through
   `P31/wdt:P279*` against the roots. Exact `VALUES` only: a `STRSTARTS` or `REGEX` over `P856`
   scans every value.

The query, as run for this plan, cut to two hosts:

```sparql
SELECT ?h ?i ?root WHERE {
  VALUES ?root { wd:Q484652 wd:Q79913 wd:Q327333 wd:Q2659904 wd:Q6256 wd:Q3918 wd:Q31855 wd:Q4671277
                 wd:Q1328899 wd:Q11032 wd:Q192283 wd:Q1153191 wd:Q5292 wd:Q171 wd:Q125361038
                 wd:Q43229 }
  { VALUES (?h ?d) { ("en.wikipedia.org" "wikipedia.org") ("w3.org" "w3.org") }
    ?i wdt:P13337 ?d }
  UNION
  { VALUES (?h ?u) { ("en.wikipedia.org" <https://en.wikipedia.org/>) ("w3.org" <https://www.w3.org/>) }
    ?i wdt:P856 ?u }
  ?i wdt:P31/wdt:P279* ?root .
} GROUP BY ?h ?i ?root
```

A host can match several items: nytimes.com gives the newspaper and a website item, nist.gov the
institute and one of its officers. The host takes the highest-priority kind across all of them,
with Q79913 applied per item, and the tags of all of them.

Search must not wait long on Wikidata. `classify(hosts, deadline)` returns what the cache and
rules know at once, runs the misses on a worker as favicons do (one flight per host, so two runs
asking for one host send one query), and waits at most `DEADLINE_SECONDS` (5). A host still
unresolved is `unknown` for this pick and is known by the next. Lists, rules and marks answer at
once, so a host Wikidata alone would make `avoid` is the only one that can be read before it is
known; see [Never reading an `avoid` site](#never-reading-an-avoid-site). A failed or rate-limited query
logs a warning, stores no miss, and backs off by `Retry-After`.

With `wikidata = false`, steps 6 and 7 are skipped. Wikidata sees the domains of the results a run
considers, which is new outgoing traffic, so the setting and the endpoint are documented next to
the SearXNG ones.

## Where the tier is used

| Where | Today | With source trust |
| --- | --- | --- |
| `search()` pick, `research/search.py` | RRF of SearXNG score, relevance, novelty | A fourth ranking by tier, `TRUST_WEIGHT` (0.5). An `avoid` result is dropped before ranking. |
| `Corpus.leads_for` | relevance, novelty, affinity, related bonus | `+ TRUST_BONUS[tier]`; `avoid` leads are not candidates. |
| `Corpus._affinity` | corroborated claims over pages | Each corroboration counts by the corroborating site's weight: `high` and `normal` 1, `unknown` 0.5, `low` 0.25, `avoid` 0. |
| `Corpus.site_query` | any site at `SITE_AFFINITY` | Only a site of tier `high` or `normal`, and at most `SITE_SEARCHES` (2) a run. |
| `Frontier.merge`, `corroborate.neighbours` | different `domain()` | Different registrable domain, and the quotes are not copies (phase 1). |
| `Corpus.recall` and `PageStore.search` | every cached piece | No piece of an `avoid` site, though `pages.db` keeps it. |
| Citations, sites, the graph | favicon and host | Plus kind, tags and tier. |

### Never reading an `avoid` site

An `avoid` page can poison the claims, so it is kept out at every way in:

1. `search()` drops `avoid` results before ranking, even when nothing else is left for the query.
2. `leads_for` leaves out `avoid` leads; `fetch` is never called for them.
3. Recall skips `avoid` sites, in the run's corpus and in `pages.db`.
4. A host found `avoid` after it was read (Wikidata answered late, or the user marked it during the
   run) has its claims removed from the frontier, from every `corroborated_by` and
   `contradicted_by`, and from the questions' answers, before the write stage. The run logs it and
   the step shows how many claims were dropped.

STORM is covered through `is_valid_source` of its retriever (`runners.py`), which drops `avoid`
results.

The planner's `QUERY_SHAPES[2]` ("add site: and the official domain") stays: a site the planner
names is searched once, and its results are ranked like any other.

The writer and the verification cascade do not change in this plan. A sentence whose claims all
come from `low` sites is marked in the result, as `[unverified]` is: that is data for
the reader, not a rewrite.

## Build order

Each phase is one subagent's task. A phase lists the files it owns; two phases that share a file
run one after the other. Each phase ends with its tests passing (`uv run pytest`, and
`npm test` in `web/` for phase 4), and with the docs it lists updated in the same change.

### Phase 1: stop farms corroborating each other

No classification needed. It fixes the run above on its own.

- `research/corroborate.py`: add `registrable(url)` with `tld.get_fld(..., fail_silently=True)`,
  falling back to `domain()`. Add `tld` to `pyproject.toml` as a direct dependency.
- `methods/ledger/frontier.py` `merge`, and `corroborate.neighbours`: compare `registrable()`, and
  do not count a repeat whose normalized quote equals the held claim's, or contains it, as a
  corroboration. A copied paragraph is one source.
- `research/corpus.py`: `SITE_SEARCHES` (2) a run in `site_query`.
- Tests: `tests/test_frontier.py`, `tests/test_verification.py`, `tests/test_leads.py`. One test
  per rule: subdomains of one site, a copied quote, the third site search refused.
- Docs: `docs/research.md` "Recall and leads" (the `SITE_AFFINITY` paragraph) and
  "Corroborate and contradict" ("Pages of one site, with `www.` dropped, are one source").

### Phase 2: the `sources` module

- New `research/sources.py`: `Source`, `tier()`, `SourceStore` (`sources.db`, tables `sources`
  and `marks`), the suffix rules, the Wikidata client, `classify(hosts, deadline)`, `mark(host,
  value)` and `unmark(host)`.
- New `research/sources.toml`: the bundled list, with a comment line per entry saying why.
- `config.py`: `SourceSettings` (`enabled` false, `wikidata` true, `endpoint` the WDQS URL, `ttl_days` 30,
  `miss_ttl_days` 7, `allow` and `deny` lists, `lists` the download URLs), registered in
  `_SECTIONS`; `Settings.sources_db`. With `enabled` false nothing is built: no store, no
  download, no worker. `kvasir.example.toml`: a commented `[sources]` block.
- Downloaded lists are fetched into `data_dir`, refreshed after `ttl_days`, and parsed from
  hosts, uBlacklist or CSV lines. Each entry of `lists` is `<label>=<url>`, the label a kind or a
  tag (`ai_generated`, `content_farm`, `predatory`). The default is CISA `current-full.csv` as
  `government`; the AI blocklist is left out, as the design rules say.
- Tests: new `tests/test_sources.py` with a fake WDQS (an `httpx.MockTransport`, as
  `tests/test_favicons.py` fakes providers): the tier table row by row, the `europa.eu` case,
  ISO as `standards` and WHO as `intergovernmental`, a host matching several items,
  `en.wikipedia.org` resolved through `wikipedia.org`, the deadline, a 429, cache expiry,
  `wikidata = false`, a mark beaten by a config `deny`, a mark surviving cache expiry.
  `tests/test_config.py` for the section.
- Docs: `README.md` settings table and a "Source trust" paragraph next to the SearXNG setup;
  `docs/architecture.md` storage (`sources.db`) and the outgoing-traffic list.

### Phase 3: use the tier in research (after phases 1 and 2)

- `research/search.py`: the trust ranking and the `avoid` drop. `research/corpus.py`: trust in
  `leads_for`, weighted `_affinity`, the tier gate in `site_query`, `avoid` sites out of recall.
  `research/pages.py` `PageStore.search` takes the hosts to exclude. The corpus gets the
  classifier in its constructor; with none (`enabled` false), every site is `unknown` and
  behaviour matches phase 1.
- The purge of [Never reading an `avoid` site](#never-reading-an-avoid-site), step 4: a
  `Frontier.drop(hosts)` called before the write stage in both methods, with a progress event.
- Wire the classifier where the corpus and `search()` are built: `methods/ledger/method.py`,
  `methods/graph/method.py`, `methods/ledger/brief.py`, `runners.py`.
- The sentence mark for `low` support: `research/verify.py` result plus
  `schemas/results.py`.
- Tests: `tests/test_search.py`, `tests/test_leads.py`, `tests/test_research.py`, with a fake
  classifier in `tests/fakes.py`: an `avoid` result never fetched even when it is the only one, an
  `avoid` lead never fetched, an `avoid` cached piece never recalled, a late `avoid` purged with
  its corroborations.
- Docs: `docs/research.md` "Search pick and ranking", the lead `worth` formula, the site search
  paragraph; `docs/fork-notes.md` is not affected.

### Phase 4: show it (after phase 2; parallel with phase 3)

- `schemas/results.py` `Citation` and `schemas/pages.py` `Site` and `SiteNode` gain
  `source: SourceInfo | None` (kind, tags, tier, label, qid). Filled in `research/render.py`,
  `outputs.py` and `api/pages.py`.
- `GET /v1/sources/{host}` and `DELETE /v1/sources` (clear the cache, not the marks), as
  `api/favicons.py`. `PUT /v1/sources/{host}/mark` with `allow`, `deny` or a kind, and
  `DELETE /v1/sources/{host}/mark`. A host set in the config answers 409. With `enabled` false
  the routes answer 404, as favicons do without providers; they stay registered, so the OpenAPI
  schema, and `lib/schema.d.ts` from it, always carry them.
- Web: regenerate `lib/schema.d.ts`; a `SourceBadge.svelte` beside `Favicon` in `Cite.svelte`,
  `Result.svelte`, `Searches.svelte`, `pages/Knowledge.svelte` and `SiteGraph.svelte`; the badge
  links to the Wikidata item when there is a `qid`. On `pages/Knowledge.svelte`, each site gets a
  menu to mark it `allow`, `deny` or a kind, or clear the mark; a config entry shows as locked.
  With sources off, `source` is null, so no badge and no menu. Update `web/mock/plugin.ts` and the fixtures.
- Tests: `tests/test_api_pages.py`, a new `tests/test_api_sources.py`, a `SourceBadge.test.ts`.
- Docs: `README.md` API list and UI views; `docs/architecture.md` routes.

### Phase 5: the WP:RSP overlay (optional, after phase 2)

Built behind `[sources] rsp` (false), effective only with `enabled`. One MediaWiki API call
(`generator=templates`, since the rows are in the page's transcluded subpages) fetches the wikitext
of every subpage; it is cached in `data_dir` and refreshed with the other lists. A row is
`|- class="s-<status>"` with its sites in `{{WP:RSPUSES|a.com|b.com}}`: `gr` gives `rsp_reliable`,
`nc` `rsp_mixed`, `gu` `rsp_unreliable`, `d` and `b` `rsp_deprecated`. The domains come from that
template only: a row with none, a part of a site (`a.com/news`) or the status `m` is skipped, and
Wikidata is not asked. `wikipedia.org` is skipped as well: its `gu` is for WP:CIRCULAR, an
article citing an article, which a run citing Wikipedia is not. A site in several rows takes the least reliable. The tags are added to
whatever the host otherwise resolves to, after it resolves and unless the config or a mark decided
it, so they set no kind. The page's license (CC BY-SA) is why no copy is shipped.

### Phase 6: evaluate

Run `eval/questions.toml` before phase 1 and after phase 3, including the WebAssembly question.
Run with `enabled` true. Report, per run: `site:` searches and their sites, citations by tier,
the share of claims from `unknown` sites, `avoid` pages fetched (must be 0), claims purged late, and the score from `kvasir.deep.evaluate`. The weights of this plan
(`TRUST_WEIGHT`, `TRUST_BONUS`, the corroboration weights) are first guesses until this phase.

The last phase also adds `docs/research.md` a "Source trust" section that owns the tier table and
the resolution order, links it from `README.md`, and sets this page's status line.

## Decisions

Settled 2026-11-04:

- `avoid` sites are never read, not even as a last resort: they can poison the claims.
- The whole feature is optional, off by default. Phase 1 is not part of it.
- A site can be marked both from the Knowledge page and through `allow` and `deny` in the config;
  the config wins.

Nothing is open.
