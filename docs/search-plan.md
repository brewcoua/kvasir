# Search providers: plan

Status: written 2026-11-06. Phases 1 to 8 and 10 built. Phase 9, and the fallback to Chromium in phase 8, wait on the open test under the phases. Needs [encryption-plan.md](encryption-plan.md), built,
since this plan stores API keys in `kvasir.db`.

Every search goes to one SearXNG instance (`SearXng`, `kvasir-research/src/search.rs:86`). That
brings SearXNG's limits with it: an engine that answers with a CAPTCHA is dropped and reported as
`unresponsive_engines`, with no way for a person to solve it; API keys for the engines that take
them live in SearXNG's own `settings.yml`; and one setting, `search.url`, is the whole of the search
configuration. This plan makes SearXNG one provider among several, each queried directly, each with
its own settings in the UI, and lets a person solve a CAPTCHA from the UI through a browser on the
server.

## Providers

Three groups, by what they need. The facts behind each choice are under [Reference](#reference).

| Group | Providers | Needs |
|---|---|---|
| Web, with a key | Brave, Tavily, Exa, Mojeek | An API key, set in the UI |
| Web, without a key | SearXNG, DuckDuckGo, Bing, Startpage, Mojeek (HTML) | Nothing; SearXNG needs its URL. Scraped ones are off by default |
| Specialized | Wikipedia, arXiv, Crossref, Europe PMC, Semantic Scholar, OpenAlex | Nothing, a `mailto`, or a key |

Left out, for now:

- Google Custom Search JSON API: closed to new customers, ends 2027-01-01.
- Bing Web Search API: retired 2025-08-11.
- Serper.dev: its documentation answered 404; the endpoint and parameters are known only from third
  parties. Added once confirmed.
- Kagi: $12 per 1,000 queries, and no free tier found.
- Google and Google Scholar without an API: see phase 9.
- Reddit: API access restricted and sources conflict.

Unpaywall is not a search provider: it looks up one DOI. It can add an open-access PDF link to a
result that has a DOI; that is an open question.

## Settings

Each provider is a set of fields under `search.<provider>.`, in the `settings` table like every
other field ([settings-plan.md](settings-plan.md)):

| Field | Kind | For |
|---|---|---|
| `search.<provider>.enabled` | `Bool` | Every provider. Off by default |
| `search.<provider>.api_key` | `Secret` (new) | Providers that take a key |
| `search.<provider>.weight` | `Float` | Its weight in the fusion, 1.0 by default |
| `search.searxng.url` | `Text` | Replaces `search.url` |
| `search.crossref.mailto` and the like | `OptionalText` | Provider-specific options |

`search.top_k` stays. `Field::section` splits a key at its first dot (`kvasir-config/src/fields.rs:44`),
so `search.brave.api_key` is section `search`, name `brave.api_key`; the loader builds the nested
object the section struct reads.

A provider's endpoint is fixed in code and never a setting, except SearXNG's, which takes no key.
The reason is the one that keeps the model providers' `api_base` out of the API in
[settings-plan.md](settings-plan.md): an API that could change Brave's endpoint would send its key
wherever the caller asks.

### The `Secret` kind

- `GET /v1/settings` returns `{"set": true}` or `{"set": false}` for a secret, never the value.
- `PATCH /v1/settings` with a string sets it, `DELETE /v1/settings/{key}` removes it. A `PATCH`
  without the key keeps it.
- The UI (`clients/web/src/components/ServiceField.svelte`) renders a password input that shows
  "set" or "not set" and is empty until typed in.
- The value is kept out of every `Debug` and log line, as `Provider`'s key is
  (`kvasir-config/src/lib.rs:43`).

### Setup

`search.url` is required today (`fields.rs:195`), and `/readyz` answers 503 until it is set. The
rule becomes: at least one web provider enabled, with its key or URL. `missing()`
(`kvasir-config/src/lib.rs:134`) gains that check. The setup form (`Setup.svelte`) lists the web
providers with their fields, in place of the one "SearXNG URL" input.

## Searching

```text
query -> every enabled provider not cooling down, in parallel
      -> one ranked list per provider
      -> fused by weighted reciprocal rank, duplicates merged by URL
      -> the existing ranking (relevance, novelty, trust) and pick
```

- The `Search` trait (`search.rs:74`) stays as the seam the methods call. A new `Providers` type
  implements it over the enabled providers; each provider implements a smaller trait that returns
  its rows or a typed failure.
- Fusion uses each provider's rank, not its score. Brave, Exa and Marginalia return no score, and
  scores of different engines do not compare. `SEARXNG_WEIGHT` (`search.rs:44`) becomes the
  provider's `weight`.
- Two providers returning one URL make one `Hit`. Its `snippets` (already a `Vec`) hold both
  snippets; its fused rank counts both.
- `ctx.search_started` and `search_ended` (`kvasir-run/src/ctx.rs:57`) are called once per provider
  per query, with the provider's name as the engine. The run's searches list shows each one.
- `record_search(1)` still counts one per query, so `research.max_searches` keeps meaning queries.
  Paid providers bill per provider call, which the settings page states next to each key.

### Failures

Each provider maps its errors to one type:

| Failure | Seen as | Then |
|---|---|---|
| `RateLimited { retry_after }` | 429, Tavily's `Retry-After`, DuckDuckGo's 202 | Cool down for `retry_after`, or 60 s |
| `QuotaExhausted` | Exa 402, Tavily 432/433, Mojeek `"ERROR: Daily Limit Reached."` | Cool down until the next UTC day |
| `Captcha { challenge }` | The block markers in [Reference](#reference) | Cool down until solved; a challenge is opened |
| `Unauthorized` | 401, 403 on a keyed API | Off until the key changes; reported in `/readyz` |
| `Other(String)` | Anything else | No cool down |

A query fails only when every provider fails, as `ResearchError::SearchUnavailable` today
(`kvasir-research/src/lib.rs:138`). Its strings keep the `provider: reason` form that
`FAILED_SEARCHES` and its tests read (`kvasir-ledger/src/search_loop.rs:37`).

Each provider has its own rate limiter. arXiv's terms allow one request every 3 seconds on one
connection, and Semantic Scholar's key one a second. A query does not wait on a slow limiter: the
provider sits that query out.

## CAPTCHAs

A CAPTCHA cannot be shown in the UI and its answer passed on. Its token lasts 2 minutes (reCAPTCHA,
hCaptcha) or 5 (Turnstile), is valid once, and what the engine gives in return is a cookie for the
client that solved it (`GOOGLE_ABUSE_EXEMPTION`, `cf_clearance`). A cookie the user gets in their
own browser comes from their IP, not the server's. So the solving happens in a browser on the
server, and the user drives it from the UI:

1. A provider sees a block page and fails with `Captcha`. It cools down. The run goes on with the
   other providers; it never waits for a person.
2. The service opens the blocked URL in a Chromium tab and lists a challenge at
   `GET /v1/search/challenges`. The run's event stream tells the UI, which shows a notice.
3. The UI opens `GET /v1/search/challenges/{id}/live`, a WebSocket. The server sends the tab's
   frames from `Page.startScreencast` and acknowledges each with `Page.screencastFrameAck`; the UI
   sends clicks and keys, which the server replays with `Input.dispatchMouseEvent`,
   `Input.dispatchKeyEvent` and `Input.insertText`. All of these are in `chromiumoxide_cdp` 0.9.1,
   which the `chromium` feature already uses.
4. When the tab's page no longer shows the block markers, the server reads its cookies with
   `Network.getCookies`, stores them for the provider in `kvasir.db`, closes the tab and ends the
   provider's cool down.

The live endpoint drives a real browser on the server, so:

- It takes the same token as every other route.
- It acts on one tab, opened by the service, and offers no address bar. A navigation off the
  engine's host closes the tab.
- A challenge expires after 10 minutes unsolved.

Without the `chromium` feature (the default image), a CAPTCHA is reported and the provider cools
down, as today.

Two engines use proof-of-work instead of a CAPTCHA, which the client can solve itself: Mojeek's
ALTCHA (`/captcha/challenge`, then `/captcha/verify`) and Startpage's Anubis (SHA-256, difficulty 4
or less per SearXNG). No person is involved.

Whether the cookies work from `reqwest` is unknown. An engine may tie them to the browser's TLS
fingerprint, which rustls does not match. Phase 8 tests it per engine. Where it fails, the
provider's later queries go through the same Chromium instead.

## Phases

1. `Secret` field kind: parse, `GET` as `{"set": ...}`, `PATCH`, `DELETE`, the password input in
   `ServiceField.svelte`. Nested `search.<provider>.<name>` keys in the loader. Commit.
2. Providers. The provider trait, the typed failure, `Providers` with fan-out, weighted fusion, URL
   merging, per-provider limiter and cool down. SearXNG ported to it, `search.url` moved to
   `search.searxng.url` with a migration of the stored row. The test doubles in
   `kvasir-research/tests/common/mod.rs`, `kvasir-ledger/tests/ledger.rs`,
   `kvasir-graph/tests/graph.rs` and `kvasir-storm/tests/storm.rs` keep working. Commit.
3. Setup and `/readyz`: the rule above, each provider's state (`ok`, `cooling down until`,
   `unauthorized`) in place of `"searxng": bool`. `Setup.svelte` and `e2e/service.spec.ts`. Commit.
4. Brave, Tavily, Exa, Mojeek. Each tested against a `wiremock` fake of its documented request and
   answer, including its quota and rate-limit errors. One commit each.
5. Wikipedia, arXiv, Crossref, Europe PMC, Semantic Scholar, OpenAlex. Wikipedia sends a
   `User-Agent` naming kvasir and a contact, as Wikimedia's policy asks. One commit each.
6. DuckDuckGo, Bing, Startpage, Mojeek HTML, off by default. Block detection per engine from the
   markers below, Startpage's and Mojeek's proof-of-work. One commit each.
7. The settings page groups the providers, says which are scraped and that scraping is against
   those engines' terms, and gives each key's price per 1,000 queries. Commit.
8. CAPTCHA hand-off, under the `chromium` feature: challenges, the WebSocket, cookie capture, and
   the test of each engine's cookies from `reqwest`. Commit.
9. Google through the browser, if phase 8 shows a solved tab keeps answering; Google Scholar the
   same way. Commit.
10. Docs: README's search section and the quickstart without SearXNG as a must, `docs/research.md`
    (`SEARXNG_WEIGHT` at :338), `docs/architecture.md`. The `searxng/` directory stays for whoever
    runs it. Commit.

## Open questions

- Whether a solved tab keeps answering, and whether its cookies work from `reqwest`. Both need a
  real block and a person, so phase 8 tested the hand-off against a fake block page only. Phase 9
  and the fallback to Chromium wait on the answer. The UI polls `GET /v1/search/challenges` rather
  than reading a run's event stream, since a block outlives the run that met it.

- Cost. Every enabled provider is asked every query. With three paid providers a run of 50 queries
  makes 150 billed calls. A per-provider `fallback` mode, asked only when the others return too
  little, would cut that.
- Specialized providers on every query. arXiv or Crossref asked "best pizza in Naples" waste a
  call and their limiter's slot. They could be asked only for questions the planner marks as
  academic.
- Model provider keys. `KVASIR_PROVIDER_<NAME>_API_KEY` stays in the environment, for the reason
  above. Moving it to the UI means fixing each provider's `api_base` too.
- Unpaywall, to add open-access PDF links to results with a DOI.

## Reference

Gathered 2026-11-06 from the sources linked. "Unconfirmed" means no official source was read.

### Web APIs with a key

| Provider | Request | Auth | Parameters | Result fields | Price, limits, errors |
|---|---|---|---|---|---|
| Brave | `GET https://api.search.brave.com/res/v1/web/search` | `X-Subscription-Token` | `q`, `count` (max 20), `offset` (max 9), `country`, `search_lang`, `freshness` (`pd`, `pw`, `pm`, `py`, or `YYYY-MM-DDtoYYYY-MM-DD`), `safesearch`, `extra_snippets` | `web.results[]`: `title`, `url`, `description`, `extra_snippets`. No score | $5 per 1,000, $5 a month free, 50 per second. Error codes unconfirmed |
| Tavily | `POST https://api.tavily.com/search` | `Authorization: Bearer` | `query`, `max_results` (max 20), `search_depth`, `topic`, `time_range`, `start_date`, `end_date`, `include_domains`, `exclude_domains`, `include_raw_content`, `country` | `results[]`: `title`, `url`, `content`, `score`, `raw_content`, `published_date` | 1,000 credits a month free; $0.008 a credit, advanced 2. 100 a minute (dev key), 1,000 (production). 429 with `Retry-After`, 432, 433 |
| Exa | `POST https://api.exa.ai/search` | `x-api-key` | `query`, `type`, `numResults` (max 100), `includeDomains`, `excludeDomains`, `startPublishedDate`, `endPublishedDate`, `contents` | `results[]`: `url`, `title`, `publishedDate`, `author`, `text`, `highlights`. No score, no paging | $10 a month free; $4 to $7 per 1,000. 402 out of credits, 429 |
| Mojeek | `GET https://www.mojeek.com/search?fmt=json` | `api_key` parameter | `q`, `s`, `t`, `since`, `before`, `lb`, `rb`, `fi`, `fe` (25 each), `site`, `safe` | `response.results[]`: `url`, `title`, `desc`, `score`, `date` | £2 per 1,000 (5 per second), card required. Error in `response.status`. Startup tier may cache results 1 hour |
| Marginalia | `GET https://api2.marginalia-search.com/search` | `API-Key` | `query`, `count` (max 100), `page`, `dc` | `results[]`: `url`, `title`, `description` | Free key by email, non-commercial, CC-BY-NC-SA. Not in the first set |

Sources: https://api-dashboard.search.brave.com/app/documentation/web-search/get-started,
https://brave.com/search/api/, https://docs.tavily.com/documentation/api-reference/endpoint/search,
https://docs.tavily.com/documentation/api-credits, https://docs.exa.ai/reference/search,
https://exa.ai/docs/reference/pricing,
https://www.mojeek.com/support/api/search/request_parameters.html,
https://www.mojeek.com/support/api/search/json_response.html,
https://www.mojeek.com/services/api.html, https://about.marginalia-search.com/article/api.

Brave's page says both "$5 in free credits every month" and "up to $20 in free calls every month".

### Specialized

| Source | Search | Auth | Limits | Notes |
|---|---|---|---|---|
| Wikipedia | `GET /w/rest.php/v1/search/page?q=&limit=` (max 100) | None; a `User-Agent` with contact | Requests one at a time, back off on `ratelimited` | `excerpt`, `description`, `thumbnail`. The Action API's `list=search` takes CirrusSearch operators |
| arXiv | `GET http://export.arxiv.org/api/query?search_query=` | None | One request every 3 seconds, one connection, across all your machines | Atom. Prefixes `ti:`, `au:`, `abs:`, `cat:`. Links to the abstract page and the PDF |
| Crossref | `GET https://api.crossref.org/works?query=` | `mailto` for the polite pool | Since 2025-12-01, lists: 1 a second, 1 at a time; with `mailto` 3 and 3 | `rows` max 1,000, `cursor=*` |
| Europe PMC | `GET https://www.ebi.ac.uk/europepmc/webservices/rest/search?format=json` | None | Unconfirmed | `doi`, `title`, `pubYear`, `isOpenAccess`, `hasPDF`; `cursorMark` paging |
| Semantic Scholar | `GET /graph/v1/paper/search` | `x-api-key`; without, a shared pool | 1 a second with a key | Max 100, offset, 1,000 results at most. `fields=` needed beyond id and title |
| OpenAlex | `GET https://api.openalex.org/works?search=` | A key, required since about 2026-02 (the help page still says optional) | 30 a second with a key | $1 per 1,000 searches; a free key has $1 a day. `AND`, `OR`, `NOT`, phrases, `~N` |

Sources: https://www.mediawiki.org/wiki/API:REST_API/Reference,
https://www.mediawiki.org/wiki/API:Etiquette, https://info.arxiv.org/help/api/user-manual.html,
https://info.arxiv.org/help/api/tou.html,
https://crossref.org/blog/announcing-changes-to-rest-api-rate-limits/,
https://api.semanticscholar.org/graph/v1/swagger.json,
https://www.semanticscholar.org/product/api/tutorial,
https://help.openalex.org/how-to-use-the-api/rate-limits-and-authentication,
https://casrai.org/news/openalex-api-keys-mandatory-usage-based-pricing-2026.

### Scraped engines

From SearXNG's engine sources (https://github.com/searxng/searxng/tree/master/searx/engines), read
through a summarizing fetch, not byte for byte. Check each against the file before building.

| Engine | Request | Block seen as |
|---|---|---|
| DuckDuckGo | `POST https://html.duckduckgo.com/html/`; `vqd` from `input[name=vqd]` needed from page 2 | `form#challenge-form`; 303 gives nothing. Third parties also report a 202 throttle and `anomaly-modal__`, `/anomaly.js` |
| Bing | `GET https://www.bing.com/search?q=&setlang=&cc=`; result links are `ck/a?...&u=a1<base64url>` | SearXNG detects none |
| Startpage | `sc` token from the form on the home page, then `POST /sp/search`; results are React JSON in the page | `id="anubis_challenge"` (proof-of-work); a redirect to `/sp/captcha` |
| Mojeek | `GET https://www.mojeek.com/search?q=` | ALTCHA via `/captcha/challenge`, `/captcha/verify`, cookie `chllg` |
| Google | `GET https://www.google.com/wml/search`, a Nokia `User-Agent`, `chrome99_android` TLS impersonation | Host `sorry.google.com` or path `/sorry`, a 302, or a body under 2,000 characters with `/sorry/`. Broke between 2026-05 and 2026-08 (SearXNG issue #6171, fixed by #6546) |
| Google Scholar | `GET https://scholar.google.com/scholar?q=&as_sdt=2007` | Redirect to `/sorry/index?continue`, or `form#gs_captcha_f` |

Google has required JavaScript for its normal results page since about January 2025, and Whoogle
was archived on 2026-08-14. Scraping Google, Bing or DuckDuckGo is against their terms; the
operator turns it on and carries that.
