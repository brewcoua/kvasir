# Rust migration: plan

Status: phase 0 done in Python, phases 1 to 3 passed, phase 4 passed for ledger and graph. Written 2026-11-05 against commit `b34fd37`.

kvasir is one Python process. Its work is blocking, because dspy, litellm and the STORM fork are
synchronous, so a run is a thread with nested thread pools under an asyncio server. This plan
replaces that process with one Rust binary, keeps the HTTP API, the web UI and the Open WebUI pipe
working, drops Co-STORM, and makes STORM an ordinary method on the shared pipeline.

Rust is for type safety, cancellation and atomicity. It will not make runs much faster: a run spends
its time waiting on models and the network.

## Decisions

| Decision | State | Reason |
| --- | --- | --- |
| Rewrite the service in Rust. Keep `web/`. Keep `owui/pipe.py` in Python. | Settled | Open WebUI imports the pipe into its own process. It talks only HTTP. |
| Drop Co-STORM: `/v1/session*`, `sessions.py`, `conversation.py`, its pipe model, `sessions.*` settings. | Settled | Removes 2,848 lines, `dataclass.py` (849) and 16 of 48 signatures. |
| Existing session files are not migrated. | Settled | They go with Co-STORM. |
| STORM stops being a fork and becomes `kvasir-storm`, written against kvasir's own types. | Settled | The rewrite removes the reason to stay close to upstream. Credit it in the README. Keep its MIT notice, see [Licensing](#licensing). |
| No reverse proxy between Rust and Python. Build beside `src/`, cut over once. | Settled | With Co-STORM gone and STORM integrated, a proxy would serve one method for one phase, and both processes would share SQLite files. |
| Stealth rendering stays in Rust, with no Node or Python sidecar. | Settled | kvasir uses little of what Patchright patches. See [Rendering](#rendering). Spike 3 decides between no fork and a small fork. |
| Port first, change behaviour after. | Settled | Evaluation differences can then be blamed on the port. Cancellation and full-page STORM come after cutover. |
| Prompts match dspy's rendering byte for byte until cutover. Whether to move to JSON-only prompts is decided on evaluation in phase 7. | Settled | A format change during the port would be indistinguishable from a port bug. |
| Stored `costorm` runs are deleted at migration. | Settled | History is for debugging, and none is known to exist. |
| Python feature work stops at the end of phase 0. | Settled | Otherwise each new feature is written twice. |

## What the rewrite fixes

| Problem today | Where | After |
| --- | --- | --- |
| A run cannot be cancelled. A disconnect leaves its thread running. | `api/streaming.py:108` | `CancellationToken` in `RunCtx`. Dropping a task stops its IO at the next await. |
| Identity travels by contextvars, and pools must copy them. Embedding usage needs a litellm callback detour. | `storm/runtime.py`, `storm/encoder.py` | Explicit `RunCtx` argument and `tracing` spans. The embedding response carries its own usage. |
| Unlocked shared state: usage counter, slot limit, host map. | `storm/lm.py:84`, `api/settings.py:191`, `storm/runtime.py:135` | Compile errors unless guarded. |
| Replacing the fetch semaphore while permits are held loses them. | `storm/runtime.py:47` | `KVASIR_MAX_THREADS` is read at startup, so the semaphore is built once and never replaced. |
| A permit must be released by hand on every path. | `api/streaming.py` | An RAII guard moves into the task. |
| DNS can rebind between the public-address check and the connection. | `docs/architecture.md` decisions | A custom `reqwest` resolver returns only public addresses, and the connection uses what it returned. |
| The STORM fork is excluded from lint and type checks. | `pyproject.toml` | No exclusions. |

These do not fix themselves. Rust compiles them happily, so each needs its own design:

| Logic problem | Where | Plan |
| --- | --- | --- |
| A model-chosen URL is fetched with no SSRF check, following redirects. | `persona_generator.py:12,82` | Fix in Python in phase 0. In Rust it goes through the guarded client. |
| `kvasir.db` uses the default rollback journal and a 5 second busy timeout. | `runs/store.py:64`, local file reports `delete` | WAL and 30 seconds, like the other databases. |
| No schema versions. All four databases report `user_version` 0. | Local files | Migrations, see [Data migration](#data-migration). |
| A settings change must commit and apply together. | `api/settings.py` | One async mutex around validate, transaction, then snapshot swap. |
| Temp-and-rename without a directory fsync. | `research/sources.py:370` | `tempfile` persist, then fsync the directory. |

The session races found in `conversation.py:46-52` and `sessions.py:64` leave with Co-STORM.

## Workspace

A virtual manifest with a flat `crates/`, each crate named after its folder, following
[Large Rust Workspaces](https://matklad.github.io/2021/08/22/large-rust-workspaces.html). Versions
and lints live in `[workspace.dependencies]` and `[workspace.lints]`. `rust-toolchain.toml` pins the
compiler. Edition 2024.

```text
Cargo.toml
crates/
  kvasir-types     ids, RunState, Stage, Usage, Citation, Claim, requests, results; serde and schemas
  kvasir-config    Settings, environment, settings table, validation, live snapshot
  kvasir-db        open, WAL, busy timeout, migrations, spawn_blocking helper
  kvasir-llm       Llm trait, gateway client, retries, usage and cost, embeddings, signatures, roles
  kvasir-extract   HTML to markdown, links with regions, challenge checks, PDF child; no network
  kvasir-browser   Chromium rendering; optional cargo feature
  kvasir-fetch     SSRF guard, robots, politeness, normalize, pages.db, fetch_page
  kvasir-sites     source trust and favicons; sources.db, favicons.db
  kvasir-run       Run, RunRegistry, slots, watchers, RunCtx, Method trait, kvasir.db history
  kvasir-research  Search trait and SearXNG, corpus, recall, intake, verify, corroborate, write, render
  kvasir-ledger    method
  kvasir-graph     method
  kvasir-storm     method
  kvasir-api       axum routes, SSE, auth, OpenAPI
  kvasir           binary: serve, openapi, eval, pdf-text; embeds the web build
  kvasir-testkit   scripted Llm and Search, fake gateway, fixtures; dev-dependency only
web/  owui/  eval/
tests/contract/    Python, black box, see Contract
```

Arrows point at what a crate depends on. `types` and `config` are used by all of them and left out.

```mermaid
flowchart TD
    bin[kvasir] --> api[kvasir-api]
    api --> ledger[kvasir-ledger]
    api --> graph[kvasir-graph]
    api --> storm[kvasir-storm]
    ledger --> research[kvasir-research]
    graph --> research
    storm --> research
    research --> run[kvasir-run]
    research --> sites[kvasir-sites]
    research --> fetch[kvasir-fetch]
    run --> llm[kvasir-llm]
    sites --> fetch
    fetch --> extract[kvasir-extract]
    fetch --> browser[kvasir-browser]
    fetch --> db[kvasir-db]
    sites --> db
    run --> db
```

Rules:

- A crate exists for a dependency boundary or a separately replaceable part. If one ends up with a
  single user and no heavy dependency, merge it.
- Libraries return `thiserror` enums. Only the binary uses `anyhow`.
- No crate holds process-wide mutable state. Shared services are built in `kvasir` and passed in.
- Traits only where two real implementations exist: `Llm` and `Search` (real and scripted, and more
  engines later), and `Method`. The browser, extractor and stores are concrete.

### Adding a method

`Method` is not object safe and does not need to be. `kvasir-api` holds one closed enum, so the
OpenAPI union stays exact and a missing arm is a compile error. Adding a method is a crate, a variant
and a match arm.

```rust
pub trait Method {
    const NAME: &'static str;
    const CLARIFIES: bool;
    type Options: DeserializeOwned + JsonSchema + Send;
    fn stages(options: &Self::Options) -> Vec<Stage>;
    fn defaults(settings: &Settings) -> serde_json::Map<String, Value>;
    fn run(ctx: RunCtx, input: Input, options: Self::Options)
        -> impl Future<Output = Result<RunResult, RunError>> + Send;
}
```

## Design

### Runs

```rust
pub struct RunCtx {
    pub run: Arc<Run>,                // state, stage, usage, progress, watchers
    pub settings: Arc<Settings>,      // the snapshot taken when the run started
    pub services: Arc<Services>,      // llm, search, fetcher, sites, pages
    pub cancel: CancellationToken,
}
```

This replaces every contextvar: run id, kind, stage, usage sink, current run, retry budget and
`max_age_hours`. The retry budget and maximum age become call parameters. Log lines get run id, kind
and stage from a `tracing` span around the run.

- **Order in `streamed` stays:** register, claim a slot or record `rejected` and answer 429, send the
  `run` frame, run, set the result, then end the run.
- **Slots:** a small `Mutex<{limit, active}>` with an RAII guard, tried without waiting. A plain
  `Semaphore` cannot shrink cleanly when `runs.max_concurrent` changes live.
- **Fan-out:** one `tokio::spawn` per run and `JoinSet` inside it. Width comes from a semaphore
  sized by `KVASIR_MAX_THREADS`. Holding a permit across an inner wait still deadlocks, so permits stay
  at leaves, as `docs/fork-notes.md` describes.
- **Watchers:** a broadcast channel. Subscribe before taking the snapshot, as today. A watcher that
  lags gets a new `snapshot` rather than missed events, which the frame already supports.
- **Run state:** `std::sync::Mutex` held for short, non-await sections. Clippy's
  `await_holding_lock` is denied.
- **CPU work:** extraction runs under `spawn_blocking`, and PDF runs in a child process.

### Signatures and the LLM client

No mature DSPy port exists, and no optimizers or `.compile` are used here. What must be kept from
dspy is small: ChatAdapter rendering, lenient parsing, and the JSON fallback.

```rust
pub trait Signature {
    const INSTRUCTIONS: &'static str;   // from an embedded prompt file
    type Input: Serialize + JsonSchema;
    type Output: DeserializeOwned + JsonSchema;
}

let findings = ctx.predict::<Intake>(Role::Intake, input).await?;
```

- **Count:** 32 signatures remain: research 6, clarify 1, evaluate 1, ledger 10, graph 3, STORM 11.
- **Rendering:** field order and descriptions come from the schema. `ChainOfThought` becomes a
  `reasoning` output placed first.
- **Parsing:** `[[ ## field ## ]]` sections, then JSON with `response_format`, as
  `dspy/adapters/chat_adapter.py` falls back today. A failure is `LlmError::Parse`, which callers
  treat as they treat `AdapterParseError` now.
- **Coercions** become type-level: `moderate` in `Finding.confidence` is `#[serde(alias = "moderate")]`.
- **Prompts** live in embedded data files, so editing one does not touch logic.
- **No proc macro at first.** Add one only if 32 hand-written `impl`s prove to be the bigger cost.

The client is a thin layer over `reqwest`: chat, embeddings and model listing. Not `async-openai`:
its `chat().create` returns the parsed body only, and kvasir needs response headers and
non-standard usage fields (spike 2):

- Cost, in order: the `x-litellm-response-cost` header, then `usage.cost`, or Bifrost's
  `prompt_cost` and `completion_cost`. Absent, 0.0. dspy's own `response_cost` for known models goes
  away, so a model behind a provider reporting none now costs 0.0. This is a behaviour change.
- Reasoning tokens and returned thinking.
- Retries for 429, 5xx, timeouts and transport errors only, for up to 1000 seconds, or 30 for
  `/v1/tasks` and `/v1/clarify`. A bad request or context overflow fails at once.
- `api_key` and `api_base` per provider, never read from the environment by a library default.

### Fetching

The order in `docs/research.md` stays: cache, static, check, render, extract.

- **Client:** `reqwest` with rustls. A custom resolver refuses non-public addresses. A literal IP
  in a URL skips the resolver, so the URL guard checks those. A second client without the guard serves
  the first request of a `custom` favicon provider, as today.
- **Politeness:** per-host limit of 2, interval, `Retry-After`. `reqwest` has only an idle-pool limit
  per host, so this is ours: a host map of small semaphores and next-allowed times, pruned when idle.
- **Normalize:** a port of `courlan.normalize_url(strict=True)` plus `fetch.py:552`. Strict mode is
  an allowlist: `aid article_id artnr id itemid objectid p page pagenum page_id pid post postid
  product_id lang language`, keys sorted. So no tracking-parameter list is needed.
  Use the `url` crate, whose output differs in places (it keeps punycode where courlan decodes it),
  and re-key `pages.db` once at migration. Matching Python byte for byte has no value after that.
- **Registrable domain:** `psl` replaces `tld.get_fld`. Check that private suffixes match in phase 3.
- **Extraction:** decided by spike 1. Scripts, menus and interactive parts are cleaned and links read
  with `dom_query`, as bs4 does now.
- **PDF:** a child process, `kvasir pdf-text`, reading stdin with a timeout and a memory limit. A
  panic can be caught, but an allocation abort kills the process, and blocking parsing cannot be
  cancelled. A child solves all three. PyMuPDF stays ruled out as AGPL, so `cargo-deny` refuses AGPL.
- **`pages.db` failures** are logged and the run carries on, as today.

### Rendering

Patchright changes the Playwright driver. It has 36 patch files in `driver_patches/`, and most exist
to keep Playwright's features working once `Runtime.enable` is gone. `research/browser.py` uses few
of those features: launch, one context per host, a resource filter, `goto`, a wait for network idle,
one `evaluate`, close. It uses no init scripts, bindings, locators or shadow roots.

| Patchright change | Needed here | With `chromiumoxide` 0.9 |
| --- | --- | --- |
| Remove `--enable-automation`. It is in `DEFAULT_ARGS`. | Yes | Config: `disable_default_args()`, then pass our own list. |
| Add `--disable-blink-features=AutomationControlled`. | Yes | Config: `arg()`. |
| Remove `--disable-popup-blocking`, `--disable-component-update`, `--disable-default-apps`, `--disable-extensions`. | Yes | Config: left out of our list. |
| Do not call `Runtime.enable`. | Yes | **Not config.** `FrameManager::init_commands` sends it on attach. Send `Runtime.disable` after attach and before navigating (spike 3). |
| Evaluate in an isolated world. | Yes, for `_CLEAN` | `Page.createIsolatedWorld`, then `Runtime.evaluate` with its context id. Reading and removing DOM nodes works from there. |
| Disable the Console API. | Only if sent | Attach does not visibly send `Console.enable`. `Log.enable` is hardcoded. Check in spike 3. |
| Inject init scripts through routes. | No | Not used. |
| Closed shadow roots, locators, bindings. | No | Not used. |

Network idle is not awaited by `chromiumoxide`: it records `networkIdle` lifecycle events but only
navigation's `load` is waited for. Listen for the event, with the 2 second limit. Resource blocking
uses `Fetch.requestPaused`. Contexts use `Target.createBrowserContext`, capped at `MAX_CONTEXTS`.

Neither Patchright nor this changes a browser fingerprint. It removes protocol leaks and automation
flags, and `fetch.stealth` keeps that meaning. `chaser-oxide` fixes this in a chromiumoxide fork, but
its README warns that `raw_page().evaluate()` still triggers the leak, so it is a reference rather
than a dependency.

### STORM as a method

`kvasir-storm` keeps the algorithm and prompts and none of the framework. The stages stay `research`,
`outline`, `article`, `polish`.

| Stage | Signatures | Runs |
| --- | --- | --- |
| Personas | `FindRelatedTopic`, `GenPersona` | Related page contents through the guarded client |
| Conversations | `AskQuestion`, `AskQuestionWithPersona`, `QuestionToQuery`, `AnswerQuestion` | One conversation per persona, side by side |
| Outline | `WritePageOutline`, `WritePageOutlineFromConv` | Draft, then refined from the conversations |
| Article | `WriteSection` | Sections side by side, snippets picked by embedding |
| Polish | `WriteLeadSection` | Optional |

Gone with the fork:

- The file handoff: scratch directory, `FileIOHelper`, `outputs.py`, `url_to_info.json`, resume
  files, `run_config.json`, `llm_call_history.jsonl`, `truncate_filename`.
- `Engine`, `LMConfigs`, the abstract modules in `interface.py`, `Retriever`, `LoggingWrapper`, both
  callback base classes, `ContextThreadPoolExecutor`, `DSPY_CACHEDIR`.
- `GatewayModel`, `Encoder` and `SearXNG` move to `kvasir-llm` and `kvasir-research`.

`ArticleResult` is built from typed values, with a citation map from URL to number in one place, so
the rule about `url_to_unified_index` against `citation_uuid` disappears.

Correctness fixes in `docs/fork-notes.md` are requirements. Each test that pins one
(`test_storm_correctness.py`, `test_storm_concurrency.py`) gets a Rust test: keep sibling query
results when one query fails, drop a failed expert turn instead of adding a canned apology, keep the
empty-retrieval guard, write a section when no information is found, follow SearXNG redirects, never
serialise credentials.

Parity keeps STORM on search snippets. Reading full pages through `fetch_page` and `corpus`, and
taking source trust into account, is the natural next change, after cutover.

### Licensing

STORM is MIT, and MIT asks for the notice in substantial portions. Translated code and copied prompts
may count, so a README mention alone may not cover it. Keep `crates/kvasir-storm/LICENSE` and a
`NOTICE` naming the source commit `fb951af`, beside the README credit. The same applies to any
extractor ported from trafilatura. This is my reading, not legal advice.

## Dependencies

Versions are from crates.io on 2026-11-05. `Cargo.lock` is committed, and Renovate keeps them current.

| Need | Crate | Replaces | Assessment |
| --- | --- | --- | --- |
| Runtime | `tokio` 1.54, `tokio-util` 0.7, `futures` 0.3 | asyncio, threads | Production |
| Server | `axum` 0.8, `tower-http` 0.7 | FastAPI, uvicorn | Production |
| OpenAPI | `schemars` 1.2 | FastAPI schema | Production. Fits `openapi-typescript`, tested in phase 4. |
| Client | `reqwest` 0.13, rustls | httpx | Production |
| Retries | `backon` 1.6 | backoff | Production |
| Serialisation | `serde` 1.0, `serde_json` 1.0 | pydantic | Production |
| JSON repair | `dsrust-json-repair` 0.1.0-alpha.3, pinned exactly | json_repair | Alpha; passed spike 2 |
| Parsing HTML | `dom_query` 0.29 | bs4 | Production |
| Extraction and markdown | `rs-trafilatura` 0.2 | trafilatura | Passed spike 1. Weak on a few pages, so pin it and keep a corpus test. |
| URLs | `url` 2.5, `psl` 2.1 | courlan, tld | Production |
| PDF | `pdf_oxide` 0.3 | pypdf | Passed spike 1, in a child process |
| Browser | `chromiumoxide` 0.9 | playwright, patchright | Usable; spike 3 |
| SQLite | `rusqlite` 0.41, bundled | sqlite3 | Production. `rusqlite_migration` 2.6 needs rusqlite 0.40, so `kvasir-db` runs its own `user_version` steps. |
| Atomic files | `tempfile` 3 | `os.replace` | Production |
| Settings swap | `arc-swap` 1.9 | module globals | Production |
| Regex | `regex` 1.13; `fancy-regex` 0.20 only where lookaround is needed | re | Production |
| CLI, logs | `clap` 4.6, `tracing` 0.1, `tracing-subscriber` 0.3 | argparse, logging | Production |
| Errors | `thiserror` 2, `anyhow` 1 | | Production |
| Web assets | `rust-embed` 8 | StaticFiles | Production |
| Tests | `wiremock` 0.6, `insta` 1.49, `proptest` 1.12, `cargo-nextest`, `cargo-deny` | pytest | Production |

Not used: `rig-core` and `genai` (more framework than needed), `dspy-rs` (last release 2025-11-14),
`sqlx` (one local file needs no async driver or compile-time database), `figment` (last release
2024-05), `wreq` (no need to impersonate TLS today), numeric crates (cosine over `Vec<f32>`).

Built in-house: signatures, gateway client, URL normalization, politeness, SSRF guard, challenge
regexes, single-flight for sites, settings loader, migrations glue. `texting_robots` 0.2.2 is the only
robots candidate and was last released in 2023, so read it before relying on it; the parser is small
enough to write if needed.

## Where the code goes

| Python | Crate |
| --- | --- |
| `schemas/` | `kvasir-types` |
| `config.py`, `settings_store.py` | `kvasir-config` |
| `storm/lm.py`, `storm/encoder.py`, `runners.py` role tables, `research/calls.py` | `kvasir-llm` |
| `fetch.py` HTML, challenge and PDF parts | `kvasir-extract` |
| `research/browser.py` | `kvasir-browser` |
| `fetch.py` network parts, `storm/runtime.py` limits, `research/pages.py` | `kvasir-fetch` |
| `research/sources.py`, `sources.toml`, `research/favicons.py` | `kvasir-sites` |
| `runs/`, `logs.py`, `methods/base.py` | `kvasir-run`, with logging setup in `kvasir` |
| `research/` except the above; `storm/rm.py` | `kvasir-research` |
| `methods/ledger/`, `methods/graph/` | `kvasir-ledger`, `kvasir-graph` |
| `methods/storm/`, `storm/storm_wiki/`, the parts of `interface.py` and `utils.py` it needs | `kvasir-storm` |
| `api/` | `kvasir-api` |
| `__main__.py`, `research/evaluate.py` | `kvasir` |

## Contract

Clients must not notice the change, apart from Co-STORM.

- **Routes and JSON:** as served today. `POST /v1/runs` with a mistaken field stays refused, since
  `extra="forbid"` becomes `deny_unknown_fields`.
- **Errors:** FastAPI's 422 body, `{"detail": [{"loc", "msg", "type"}]}`, and each route's other
  bodies, as phase 0 records them.
- **SSE:** `run`, `progress`, `done`, `error` on `POST /v1/runs`, and `snapshot`, `progress`, `status`
  on `/events`. No event ids or `Last-Event-ID` exist, so none are needed. SSE frames are not in
  today's OpenAPI either (`web/src/lib/api.ts:7`), so documenting them is not a blocker.
- **`GET /v1/methods`:** returns each method's options as JSON Schema with defaults filled in, and
  `web/src/App.svelte` reads it. `schemars` and pydantic spell optional fields differently. Test this
  early, and adapt the output or the UI.
- **Auth:** token or HttpOnly cookie, and the `Sec-Fetch-Site` check.
- **FastAPI's own pages:** `/docs`, `/redoc` and `/openapi.json` are not part of the contract.
- **Types:** `pnpm types` calls `kvasir openapi`. The gate is `pnpm check`, `pnpm test` and `pnpm e2e`
  passing, not a byte-equal spec.
- **Stored data:** see [Data migration](#data-migration).

## Phases

Each phase ends at a gate. Python stays what ships until phase 6, with no new features after phase 0.

### Phase 0: prepare, in Python

1. Remove Co-STORM: routes, modules, schemas, `SessionSettings`, the pipe model, tests, docs.
2. Check the URL in `persona_generator.py` with `fetch.public` per hop.
3. Write `tests/contract/`: black-box tests in Python, using a scripted fake gateway and fake
   SearXNG over HTTP. Cover every route, error body and SSE transcript. The harness starts the
   service itself, since it has to point it at the fakes and restart it. `KVASIR_CONTRACT_COMMAND`
   names the command, and the fake site is on 127.0.0.1, so the Rust binary needs a test-only way
   past its SSRF check.
4. Add a capture script for golden data: every signature rendered through `ChatAdapter`
   (`tests/test_storm_signatures.py` already formats them) with parsed samples, a URL corpus from
   `pages.db` with `normalize` output, each database's schema, and run JSON.
   `tests/golden/capture.py` writes these. What holds research stays out of git, in `<data>/golden/`.

Gate: `uv run pytest` and `tests/contract/` pass against Python. The baseline is 993 passed, 103 of
them in `tests/contract/`.

### Phase 1: spikes, go or no-go

Throwaway code in a scratch crate. Each spike reports numbers here before phase 2.

| Spike | Method | Pass |
| --- | --- | --- |
| 1. Extraction and PDF | Run `dom_smoothie` with `htmd`, and `rs-trafilatura`, over `tests/fixtures/pages/`, plus the static pages in `pages.db`, refetched and compared with their stored trafilatura text. Run PDF candidates over a set of messy real PDFs, in a child. | All `must` and `must_not` in `pages.toml`. Median token F1 of at least 0.9 against stored text. No candidate means vendoring and fixing `rs-trafilatura`, or porting trafilatura's core. |
| 2. Prompts | Render all 32 signatures and compare with golden. Parse the golden samples and real replies stored in `kvasir.db` `calls`. Try `async-openai` for headers and cost. | Byte-equal prompts, except any listed difference. At least the parse rate dspy got on stored replies. |
| 3. Stealth | A local test page that notices `Runtime.enable` through a `console.debug` argument with an `Error` stack getter. Try in order: (a) stock with our args; (b) `Runtime.disable` after attach, before navigating; (c) a `[patch.crates-io]` fork that drops the command. Include cross-origin iframes, which attach as their own targets. | The page sees nothing, and `_CLEAN` returns the same HTML as Patchright on fixtures. Otherwise, reopen the sidecar decision. |

Gate: all three pass, or the plan changes here. Stop and reconsider the rewrite if extraction fails
and a port is not worth it.

#### Spike 1 result: pass, `rs-trafilatura` and `pdf_oxide`

Run on 2026-11-05 over 221 HTML pages and 20 PDFs refetched from `pages.db`. HTML went through the
same `_INTERACTIVE` clean-up Python does before trafilatura. Five pages were skipped by `robots.txt`
or the public-address check. Python's output on the same HTML is the fairer reference, since it
matches the stored text at a median F1 of 1.000.

| HTML | F1 against stored, median | F1 against Python, median | F1 against Python, 10th percentile | Pages below 0.5 | `must` missed | `must_not` present | Time for 221 pages |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `rs-trafilatura` 0.2.2 | 0.976 | 0.976 | 0.751 | 12 | 0 of 15 | 1 of 23 | 3.24 s |
| `dom_smoothie` 0.19 with `htmd` 0.5 | 0.918 | 0.918 | 0.661 | 13 | 1 of 15 | 2 of 23 | 0.76 s |

- Removing `#siteSub` with the other interactive selectors takes the one `must_not` miss to 0 of 23.
- The pages below 0.5 differ both ways. On `zylos.ai/research` Python kept 370 characters and Rust
  170,000. On a GitHub issue Rust kept 6,165 characters to Python's 55,168. Phase 3 should compare
  these by reading them and keep a corpus test.
- Headings, which `split` depends on: 3,425 against Python's 3,616, with 24 pages losing all of theirs.
- This ran without Python's fallback for text under `MIN_TEXT`. Port the fallback too.

| PDF, one process per file | Failed | F1 against pypdf, median | Lowest | Time for 20 files | Peak memory |
| --- | --- | --- | --- | --- | --- |
| `pdf_oxide` 0.3.78 | 0 | 0.989 | 0.743 | 2.0 s | 69 MB |
| `pdf-extract` 0.12 | 1, a panic in `adobe-cmap-parser` | 0.989 | 0.810 | 1.3 s | 72 MB |
| pypdf, for reference | 0 | | | 9.8 s | not measured |

- The lowest `pdf_oxide` score is pypdf's fault: it spaces out letters on that file. The next, 0.78, is
  `pdf_oxide`'s: it interleaves overlapping text in places ("The n aiew r-source heat pum"). Expect
  this on a minority of files.
- Forty damaged copies (truncated, or with bytes flipped) gave 38 results and 2 clean errors. None
  hung, and the slowest took 2.0 s.
- `pdfium-render` was not tried: it needs a native library, and `pdf_oxide` passes without one.
- Both chosen crates are licensed `MIT OR Apache-2.0`.
- These are the PDFs research has read so far, so they are real but not chosen for being difficult.

#### Spike 2 result: pass

Run on 2026-11-05. A scratch renderer read each signature as data (field names, type names,
descriptions, JSON Schema text exported from pydantic) and rebuilt dspy 3.3's prompts.

- **Rendering:** 32 of 32 signatures byte-equal to `ChatAdapter`, and 32 of 32 to `JSONAdapter`,
  the fallback. The four `ChainOfThought` signatures carry `reasoning` first.
- **Parsing:** 2,506 stored replies in `kvasir.db` `calls` matched a current signature by their
  prompt, 2,484 in chat format and 22 in JSON. Of the rest, 1,960 stored calls have no system
  message and 338 are from signatures that have changed since, so neither was used.

| JSON repair | Same value as dspy | Both refuse | dspy only | Rust only | Different value |
| --- | --- | --- | --- | --- | --- |
| `dsrust-json-repair` 0.1.0-alpha.3, a port of `json_repair` 0.61.7 | 2,468 | 37 | 0 | 0 | 1 |
| `llm_json` 1.0.3 | 2,449 | 37 | 19 | 0 | 1 |

- The one different value is the spike's: `Section.subsections` has `default_factory=list`, which
  the JSON Schema does not show, so the spike filled `null`. `#[serde(default)]` gives `[]`.
- The two `mode="before"` validators, `moderate` to `medium` and an unknown priority to
  `supporting`, were applied by hand and are needed.
- Use `dsrust-json-repair`. It is an alpha by one author, pinned to an older `json_repair` than the
  0.63.2 in `uv.lock`. Pin it exactly and keep these replies as a test.
- dspy parses through `json_repair`, then `ast.literal_eval`, then pydantic's lax mode. The spike
  matched lax coercion for the types the signatures use: strings, integers, booleans, literals, lists
  and objects.

#### Spike 3 result: pass, with `Runtime.disable`

Run on 2026-11-05 with Chromium 156 for every library, headless, `--site-per-process`.

The probe the plan named, a `console.debug` of an `Error` with a `stack` getter, never fired, even
under stock Playwright. Neither did getters on other properties, a `Proxy`, or
`Error.prepareStackTrace`. Getters on `message` and `name` fired with no CDP client attached, so they
are Chromium's own console formatting, not a signal. What does tell: 50 `console.debug` calls on an
array of 200,000 objects. With `Runtime.enable` the protocol serializes each, and the page can time
it.

| Client | Top page | Cross-origin frame |
| --- | --- | --- |
| No CDP client (`chromium --dump-dom`) | 0.0 ms | not reported |
| Playwright | 308.5 ms | 302.3 ms |
| Patchright | 0.3 ms | 0.2 ms |
| `chromiumoxide` 0.9.1, stock | 305.1 ms | 0.1 ms |
| `chromiumoxide` 0.9.1, `Runtime.disable` after attach, before `goto` | 0.2 ms | 0.2 ms |

- No fork is needed. Option (c) was not built.
- The frame stays clean even in stock `chromiumoxide` because it does not attach to out-of-process
  frames. `_CLEAN` reads only the top document, as it does under Patchright.
- `_CLEAN` ran in a `Page.createIsolatedWorld` context through `Runtime.evaluate`, with requests
  filtered through `Fetch.requestPaused` and a 1280x720 viewport, Playwright's default. Its HTML was
  identical to Patchright's on 13 of 13 fixtures in `tests/fixtures/pages/`.
- `Log.enable` is still sent on attach. The probe does not see it.
- One timing probe on a local page is evidence, not proof. No real anti-bot vendor was tested.

### Phase 2: foundation

Build `kvasir-types`, `-config`, `-db`, `-llm`, `-testkit`, with CI: `cargo fmt --check`,
`cargo clippy -D warnings`, `cargo nextest run`, `cargo deny check`.

Gate: golden prompts pass. `Settings` loads the same environment and settings rows as Python, and
rejects what it rejects. A live call to a local model parses, with usage recorded.

#### Phase 2 result: pass

Done on 2026-11-05.

- **Prompts:** `crates/kvasir-llm/tests/golden.rs` renders nine signatures, which between them use
  every field kind: text, integer input, boolean, literal, `list[int]`, `list[str]`, the four
  models, and `ChainOfThought`. All nine are byte-equal to dspy in both formats, and dspy's replies
  parse to the same values. `tests/golden/signatures.json` now holds the JSON format as well. The
  other 23 signatures are checked the same way as their method crates land.
- **Settings:** `tests/golden/settings.json` holds 123 cases, environment and stored, strict and not.
  Rust gives the same settings, the same warnings and the same error text for all of them.
- **Live call:** `gpt-oss:20b` on a local Ollama answered a three-field signature in the chat
  format, and the call recorded 331 prompt and 57 completion tokens. `nomic-embed-text` embedded a
  query. Ollama reports no reasoning tokens and no cost, so both were 0.
- **Databases:** the first migration of each file reproduces Python's schema exactly, FTS5
  included. A file Python left is copied to `<file>.pre-rust.bak` first. A copy of the real
  `pages.db` migrated, kept its 253 pages and 2,045 pieces, and answered a full-text query.

Differences from Python, all deliberate:

- `KVASIR_PORT` above 65535 is refused at load. Python failed later, at bind.
- A host with non-ASCII characters goes through UTS 46, where Python used IDNA 2003. They differ on
  a few characters, such as `ß`.
- Embedding requests retry like chat calls. Python did not retry them.
- Calls report to a `Sink` trait, a fourth trait beside `Llm`, `Search` and `Method`. A run is one
  implementation and `NoSink`, for a client's housekeeping, is the other.

Left for later phases: listing a provider's models moves with `api/providers.py` in phase 4. A run
must key its usage by `openai/<name>`, as Python did, since `tests/contract/shapes.json` records
those keys.

### Phase 3: reading the web

Build `kvasir-extract`, `-browser`, `-fetch`, `-sites`. Port `test_fetch.py`, `test_fetch_politeness.py`,
`test_pages.py`, `test_sources.py`, `test_favicons.py` as behaviour, not line by line.

Gate: `pages.toml` fixtures pass. A copy of the real `pages.db`, `sources.db` and `favicons.db` opens
and answers recall. SSRF tests cover redirect hops, literal IPs and rebinding.

#### Phase 3 result: pass

Done on 2026-11-05.

- **Fixtures:** `tests/fixtures/pages` and `pages.toml` pass through `kvasir-extract`.
- **Corpus:** against Python's text on the real page cache, F1 median 0.978 and tenth percentile
  0.807; precision median 0.973; recall median 0.998 and tenth percentile 0.932. Pages below that are
  truncated rather than wrong: a GitHub issue keeps 6,165 characters of 55,168, and the same holds for
  wasmruntime, nordiso, a chromedevtools Tracing page, novasolver and airarabia. `zylos.ai/research`
  goes the other way and keeps too much. Phase 6 reads these again in real reports.
- **Databases:** `crates/kvasir-sites/tests/real_copies.rs` runs on copies of the real files. Of 253
  pages all 253 read, with their 2,045 pieces, and full-text recall answered with vectors. All 716
  hosts in `sources.db` read (229 matches, 487 misses, no marks) and all 117 in `favicons.db` (97
  icons).
- **SSRF:** `kvasir-fetch` covers redirect hops to a private literal and to a name resolving
  privately, literal spellings, rebinding, the robots redirect, and the browser's request check.
  Each of the three guards was removed in turn and a test failed each time.

Differences from Python, all deliberate:

- `robots.txt` has its own RFC 9309 parser in place of `texting_robots`, which this plan named: the
  longest match wins and allow wins a tie, where `RobotFileParser` took the first match. Patterns are
  not percent-normalized.
- Extraction: only `&amp;` is unescaped in link targets; a PDF page that fails is skipped; a page
  nested over 2,000 elements is refused, for icons as well. `kvasir-extract` owns `normalize`.
- The browser runs without the request check on CDP-created targets and without site isolation, as
  spike 3 settled. `LINK_BOXES` and padding removal are gone.
- Page clients ignore proxies from the environment. The user agent ends in `reqwest/0.13`. A content
  type is compared case-insensitively.
- `Guard::Loopback` replaces turning the guard off, so tests run the same paths production does.
- Each hop of `describe` is checked against the guard, which Python missed. A downloaded list is
  written to a temporary file, synced, then renamed.

One hole remains. The browser's request check resolves a name and Chromium resolves it again, so a
name that answers a public address first and a private one second gets through. The HTTP clients are
closed by `GuardedResolver`. Python's browser had no check at all, so this is no worse than today.
Closing it means sending browser traffic through a guarded proxy, which is not planned.

`registrable` uses `psl` where Python used `tld`. They agreed on all 59 hosts of a hand-written list,
private suffixes included, but were not compared on the real hosts.

### Phase 4: runs and API

Build `kvasir-run`, `-research`, `-ledger`, `-graph`, `-api`, and the `kvasir` binary.

Gate: `tests/contract/` passes against the binary for ledger and graph. `pnpm check`, `pnpm test`,
`pnpm e2e` pass. `tests/test_owui.py` still passes. No lock held across an await. A test confirms a
client leaving mid-run does not free the slot early.

#### Phase 4 result: pass for ledger and graph

Done on 2026-11-06.

- **Contract:** `tests/contract/` against `kvasir serve --allow-loopback`: 101 of 103 pass, three runs
  in a row. The two that fail are the `storm` cases, which wait for phase 5.
- **Slot:** `test_a_client_leaving_does_not_end_the_run_or_free_its_slot` is among those that pass.
- **Web:** `pnpm types` runs `kvasir openapi`, and the e2e service is the binary. `pnpm check` and
  `pnpm test` (104 tests) pass. `pnpm e2e` passes 44 of 45. The one that fails,
  `a ledger run stacks its rows` in the mock project, fails the same way on the Python-generated
  types and never reaches the service.
- **Open WebUI pipe:** `tests/test_owui.py` passes, 25 tests.
- **Locks:** clippy denies `await_holding_lock`. The only lock held across an await is the
  `tokio::sync::Mutex` that keeps a provider's model listing from being asked twice at once.

Differences from Python, all deliberate:

- No `utoipa`. `kvasir openapi` writes `components.schemas` from `schemars` and no `paths`: the web
  imports only schemas. Which fields are optional is copied from FastAPI's document, and the
  methods' options, the run bodies and the 422 body are written as pydantic wrote them.
- A follower's `status` frame is the run as it stood when the progress event before it was
  published (`Run::follow`), where Python took it when the follower got to the event.
- Followers are unbounded channels, as Python's queues were.
- `FORWARDED_ALLOW_IPS` takes `*` or addresses; uvicorn's CIDR ranges are not read.
- An unexpected failure answers `{"message": "Internal Server Error"}` with 500, where Starlette
  sent the text alone.
- JSON logs are `tracing-subscriber`'s format, not Python's.
- `fetch_all` in `kvasir-research` collects its futures before streaming them: the lazy map kept a
  run's future from being `Send`, so it could not be spawned.

### Phase 5: STORM

Build `kvasir-storm`.

Gate: contract tests pass for `storm`. Correctness tests are ported. A live article from a local model
is read side by side with Python's.

#### Phase 5 result: pass on contract and tests, live read pending

Done on 2026-11-06.

- **Contract:** `tests/contract/` against `kvasir serve --allow-loopback`: 103 of 103 pass.
- **Signatures:** the ten STORM signatures match `tests/golden/signatures.json`.
- **Correctness:** `crates/kvasir-storm/tests/storm.rs` ports `test_storm_correctness.py`,
  `test_storm_concurrency.py` and `test_storm.py`: sibling queries survive a failed one, a failed
  answer ends the conversation without a canned apology, an empty table retrieves nothing, a run that
  finds nothing still writes an article, retrieval stays within `max_threads`, the guard on the URLs a
  model chose and on each redirect hop, and a target's `Debug` shows no key. The SearXNG redirect,
  score and unresponsive-engine tests are in `crates/kvasir-research/tests/search.rs`. The file
  handoff tests have nothing left to test: no file is written.
- **Workspace:** fmt, clippy, `cargo nextest run` (437 passed, 6 skipped) and `cargo deny` pass.
  `kvasir-storm` is MIT and carries `LICENSE` and `NOTICE`.
- **Live read:** not done yet.

Kept from upstream, though they look wrong:

- `search_top_k` changes nothing. Python's SearXNG retriever and `TopicExpert` never read it, so the
  Rust port accepts the option and ignores it too.
- When a section of `n` sources cites a number above `n`, every citation numbered `n` or more is
  removed, so the citation of the last source goes too (`without_citations_from`).

Differences from Python, all deliberate:

- Citations come from the numbering of the article returned. Python read `url_to_info.json`, written
  from the draft, which no longer matched once polishing renumbered.
- `PolishPage` is not ported: it ran only with `remove_duplicate`, which nothing set.
- An outline heading at the topic's level goes under the topic, where upstream emptied its stack and
  failed.
- Conversations and sections are collected in persona and outline order, not in completion order.
- A source's snippets keep the order they were found in, not `set` order.
- A section's queries are embedded in one call.
- The request for a related page's contents sends the service's user agent.

### Phase 6: parity and cutover

1. Run `eval/questions.toml` on both, same models. It is 20 judged questions, statistical rather than
   exact, and model variance is large, so compare distributions and read the worst reports.
2. Run a soak: several concurrent runs through a provider outage, then a restart mid-run.
3. Replace `Dockerfile` (web, cargo build, slim image; `--target browser` adds Chromium),
   `ci.yml`, `release-please-config.json` (`release-type` becomes `rust`), and `renovate.json`.
4. Remove `src/`, `pyproject.toml`, `uv.lock`, and Python tests other than the pipe's and contract.
5. Rewrite `docs/architecture.md`, `docs/research.md`, the README, `owui/README.md`. Replace
   `docs/fork-notes.md` with STORM's behaviour requirements and credit.

Gate: eval scores no worse, as judged by reading them; soak shows no leak or stuck slot; the image
starts read-only, as today's does.

### Phase 7: after cutover

Each is its own change, once parity holds: `POST /v1/runs/{id}/cancel` and a `cancelled` state;
SIGTERM marking running runs instead of waiting for the startup sweep; STORM on full pages and source
trust; rate limits per provider; SSE frames in OpenAPI; new methods; JSON-only prompts against dspy's
format, compared on `eval/questions.toml` with local models.

## Data migration

Runs on first start of the Rust binary, per database, in a transaction, driven by `user_version`.

> **Warning:** migration rewrites `kvasir.db` and `pages.db`, and Python cannot read them afterwards
> if the schema moves. The binary copies each file to `<name>.pre-rust.bak` first. To roll back, stop
> the service, restore the copies, and start the old image.

| File | Change |
| --- | --- |
| `kvasir.db` | Switch to WAL. Create `settings` if absent, since a store from before `0d89881` has none. Delete `costorm` runs, with their `calls` and `searches`. Delete `sessions.*` settings rows. A stored run that fails to parse is logged and skipped, not fatal. |
| `pages.db` | Re-key with Rust `normalize`, merging collisions by newest. Vectors stay float32 blobs. Page ids derive from the key, so check links in the Knowledge page survive. |
| `favicons.db`, `sources.db`, `sources-*.txt` | Unchanged, with a version number set. |
| `sessions/` | Ignored. Release notes say it can be deleted. |

Local data for scale: `pages.db` holds 253 pages (246 `static`, 7 `render`) and 2,045 pieces.
`kvasir.db` holds 33 runs: 19 `graph`, 13 `research`, 1 `ledger`, no `costorm`. `research` is not a
current method name, so a typed `kind` must still accept it. Test against copies of these.

## Risks

| Risk | Consequence | Mitigation |
| --- | --- | --- |
| Extraction is worse than trafilatura | Fewer, worse claims, silently | Spike 1, eval gate. Quote checks run on our own text, so they stay consistent. |
| Prompt or parse drift | Worse reports, silently | Byte-equal goldens, parse-rate test |
| Local model quirks in structured output | Calls fail more | Stored replies in the test set, the JSON fallback |
| Port takes long, Python keeps changing | Double work | Feature freeze after phase 0 |
| Prompt iteration slows on compile times | Slower research work | Prompts in data files; consider reading from disk in debug builds |
| Python's LLM ecosystem moves faster | New provider features arrive later | One wire protocol and a thin client limit what is needed |
| Stealth keeps changing | Render breaks on protected sites | A small surface, tested in CI against the local probe page |
| No public precedent | Unknown unknowns | Spikes first. No public Python-to-Rust rewrite of a deep-research agent was found. |

## Not verified

- `psl` against `tld` on real hosts. A hand-written list of 59 agreed.
- Crate choices marked production rest on release dates and download counts, not on use here.
- Races other than `storm/lm.py:84` and the Co-STORM ones were read from code by a reviewer and not
  reproduced.
