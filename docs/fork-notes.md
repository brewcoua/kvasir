# Fork notes

`src/kvasir/storm` is a fork of Stanford OVAL's `knowledge_storm`, taken at
[`fb951af`](https://github.com/stanford-oval/storm/commit/fb951af7744dab086e34962e9bc6fe878e145f83)
(2025-09-30) and modified. This page records every divergence, so that reading upstream against this
tree, or rebasing onto a later upstream, does not start from a diff of the whole package.

Read this before changing anything under `src/kvasir/storm`. Most entries exist because upstream
behaviour was wrong in a way that produced a bad article rather than an error.

Paths are relative to the repository root. Upstream is dormant: its last substantive change was in
January 2025, so this tree is not tracking a moving base.

## What was taken, and what was left

The whole package was vendored. `examples/`, `frontend/`, `setup.py` and upstream's CI were not.
Upstream uses only relative imports internally, so no import inside the tree was rewritten.

Upstream's dependencies are declared directly in `pyproject.toml` rather than pulled in through a
published `knowledge-storm` package. `src/kvasir/storm/LICENSE` and `src/kvasir/storm/NOTICE` record
the fork point and that the tree is modified. That directory is MIT, as upstream is; the rest of
kvasir is MIT OR Apache-2.0.

Trimming the unused retrievers and the ten deprecated language model wrappers is deliberately not
done yet. `src/kvasir/storm` is excluded from `ruff format --check` and from the strict lint rules
in `pyproject.toml` until then, so a fix is not buried in reformatting churn. `mypy` skips it too:
the tree is untyped.

## Configuration happens once, explicitly

Upstream configured litellm as a side effect of importing `lm.py` or `encoder.py`, twice, and opened
a disk cache under `Path.home()/.storm_local_cache` while doing it. Importing the package therefore
wrote to the filesystem, which fails outright under a read-only root, and no caller could choose a
different directory without editing the source.

`src/kvasir/storm/runtime.py` is now the only place the package configures anything process-wide.
Importing it still sets `litellm.drop_params` and `litellm.telemetry`, because those are policy for
this fork and neither touches the filesystem or the network. The cache is opened only by an explicit
`configure_cache(cache_dir)` call, which `main.lifespan` makes once at startup. A caller that never
calls it runs uncached rather than writing somewhere it did not choose.

Two other import-time mutations are gone: the `logging.basicConfig` in `interface.py`, which stole
the root logger from whatever embedded the package, and the global `httpx` logger level in
`utils.py`.

`src/kvasir/storm/__init__.py` imports no submodule, so importing the package no longer drags in
qdrant, langchain and transformers. It sets three environment defaults, because each is read by a
third-party package while that package is being imported:

- `DSP_CACHEDIR` and `DSP_CACHEBOOL`. dspy 2.4.9 creates a joblib cache directory while `dspy` is
  imported, whether or not caching is on. This is why the container needs a writable `/tmp`.
- `LITELLM_LOCAL_MODEL_COST_MAP`. Without it litellm fetches its model cost map over the network.

## Embeddings go through the gateway

Upstream had two unrelated embedding paths. `Encoder` (Co-STORM) hardcoded `text-embedding-3-small`
and required an `ENCODER_API_TYPE` environment variable.
`StormInformationTable.prepare_table_for_retrieval` (plain STORM) instead loaded a local
`SentenceTransformer("paraphrase-MiniLM-L6-v2")` from HuggingFace and scored with scikit-learn.

Both now use one `Encoder(model, api_key, api_base)`:

- No `ENCODER_API_TYPE`, no hardcoded model name, no Azure special case.
- One `litellm.embedding` call for a whole list, rather than one request per text across a thread
  pool.
- A failed or short response raises `EmbeddingError`. Upstream printed per-text failures and carried
  on, returning fewer vectors than it was given texts and misaligning the ones it did return against
  the input order. Callers index that result positionally.
- `cosine_similarity` in `encoder.py` replaces `sklearn.metrics.pairwise.cosine_similarity`, whose
  only use was this.

`STORMWikiRunner` takes an encoder and threads it through, so both engines are pointed at the
gateway the same way. This is what removed `torch`, `sentence-transformers`, `scikit-learn`, the
pytorch-cpu index and the baked HuggingFace weights from the image.

## Concurrency is bounded process-wide

Upstream nested three thread pools. Section writing fans out to retrieval, which fans out to page
fetches, and each level was sized independently at ten by default, so the worst case was their
product. Co-STORM was worse in one place and unbounded in another: `collaborative_storm/modules/article_generation.py`
hardcoded `max_workers=5`, and `warmstart_hierarchical_chat.py` passed no `max_workers` at all.

`runtime.max_threads()` now sizes every pool, from `KVASIR_MAX_THREADS`. Outbound search and page
requests take a permit from one process-wide `BoundedSemaphore`, `runtime.acquire_fetch_slot`.

The permit sits only at leaves that never wait on a future. A pool shared across nesting levels
deadlocks, because outer tasks occupy every worker while waiting on inner tasks that can never be
scheduled, and a permit held across a level that then waits on inner futures deadlocks the same way.
`WebPageHelper.urls_to_articles` acquires before submitting rather than inside the task, so a permit
bounds the threads created as well as the requests in flight.

`webpage_helper_max_threads` defaulted to `10` at five constructors in `rm.py` and again in
`WebPageHelper` itself. All of them now default to `None` and fall through to the process setting.

`knowledge_curation.py` reached into `executor._threads`, a private CPython attribute, to attach a
Streamlit script context to each worker. There is no Streamlit here, so that and its conditional
import are gone.

## Observability

Upstream had roughly 46 `print` calls across the tree and no way to see a run in progress. Each is
now a `logging.getLogger(__name__)` call. Two things still print.
`LocalConsolePrintCallBackHandler` does, because it is explicitly a console handler, and
`lm._inspect_history` does, because it is a debugging entry point called by hand and routing it
through logging would put a coloured transcript in the service's logs.

Related changes:

- `Engine.summary()` returns a dict instead of printing it.
- `logging_wrapper.log_pipeline_stage` caught every exception, printed it, and fell through, so a
  failed Co-STORM stage looked successful to the caller. It logs and re-raises.
- `logging_wrapper` formatted every Co-STORM timestamp in Pacific time through a `CALIFORNIA_TZ`
  constant. It uses UTC, which is what removed `pytz`.

`runtime.ContextThreadPoolExecutor` replaces `ThreadPoolExecutor` at all nine pool sites in the
tree. It copies the submitting thread's context into each task, so the run identity that
`kvasir.logs` puts in contextvars survives the fan-out, which is where most of a run happens.

`runtime` also carries a `UsageSink` on a contextvar. `lm.py` reports each completion with its
token counts and litellm's `response_cost`, `encoder.py` reports embedding tokens, and
`Retriever.retrieve` reports query counts. Nothing in the tree imports `kvasir`; with no sink
installed the calls are no-ops. `kvasir.runs.Run` is the sink the service installs.

Article generation and polishing had no callbacks at all. The last callback of an upstream run was
`on_outline_refinement_end`, after which the longest part of the run happened silently.
`storm_wiki/modules/callback.py` gained `on_article_generation_start`,
`on_section_generation_start(section)`, `on_section_generation_end(section)`,
`on_article_generation_end`, `on_polish_start` and `on_polish_end`. `run_article_polishing_module`
took no `callback_handler`; it does now.

`STORMWikiRunner.run()` calls `post_run()` itself when every stage ran, rather than relying on the
caller to remember.

## Error handling and correctness

- Bare `except:` at three sites. Two were narrowed. The one in `storm_dataclass.from_outline_str`
  guarded `str.split` and `str.strip`, which do not raise, so it was deleted.
- `Retriever.retrieve` used `executor.map`, which re-raises the first failure while iterating and so
  discarded every sibling query's results with it. It uses `as_completed` and keeps the successes.
- `knowledge_curation.TopicExpert.forward` substituted a canned apology into the research corpus on
  any failure, which then entered the conversation log as if the expert had said it. It logs the
  failure and drops the turn. The second canned answer in that file, for genuinely empty retrieval,
  is upstream's deliberate anti-hallucination guard and is kept.
- `LitellmModel` was the only live language model wrapper and the only one without a
  `@backoff.on_exception`. All seven that had one are on classes deprecated after upstream v1.1.0.
  It now retries connection errors, rate limits, timeouts and 5xx. A bad request, an auth failure or
  a context-length overflow is deterministic and still fails immediately.
- `FileIOHelper` writes to a temporary sibling and renames, sets `encoding="utf-8"`, and round-trips
  correctly through `load_str`, which used to re-join already-newline-terminated `readlines` and
  double every newline. `dump_json` raises rather than substituting `"non-serializable contents"`.
- `storm_wiki/engine.py` used `assert` for missing resume files. Assertions vanish under `python -O`.
  It raises `FileNotFoundError`, and `run()` raises `ValueError` when asked to do nothing.
- `interface.py` defined `Information.__hash__` twice. The first was dead and is gone.
- `storm_wiki/engine.py` hardcoded `disable_perspective=False`, ignoring the argument.
- `DiscourseManager._parse_expert_names_to_agent` unpacked `expert_name.split(":")` into two names.
  Model output is `"Name: description"`, and descriptions routinely contain a colon of their own, so
  a second colon raised `ValueError` and failed the turn. It uses `str.partition`, which also
  handles output with no colon at all.

## Co-STORM session restore

`CoStormRunner.from_dict` carried its own `# FIXME`. It discarded the serialised language model
configuration, re-ran `CollaborativeStormLMConfigs.init()` from `OPENAI_API_TYPE`, and passed no
`rm`. `init()` is the method that hardcodes `api_base=None` against `gpt-4o-2024-05-13`, so a
restored session billed an OpenAI account directly and retrieved through `BingSearch`. None of that
surfaced as an error.

`from_dict` now takes `lm_config`, `encoder` and `rm` and uses them. How to reach a model is current
configuration, not saved state, so `kvasir.runners.load_costorm_runner` passes what the current
settings say. Only conversation state comes from the file. `runner_argument` is restored, since it
shaped the conversation already in the file.

`rm` is required rather than defaulted to `BingSearch`, in `CoStormRunner.__init__`, in `from_dict`,
and in `collaborative_storm_utils._get_answer_question_module_instance`, which held a second copy of
the same fallback. All three call sites of that one already passed a retriever.

## Upstream behaviour kvasir relies on

These are unchanged, and are the parts of upstream's shape that kvasir's own code is written
against.

### There are two unrelated `BaseCallbackHandler` classes

`storm_wiki.modules.callback.BaseCallbackHandler` and
`collaborative_storm.modules.callback.BaseCallbackHandler` share a name and nothing else. They have
different methods and neither inherits from the other. Import the one matching the engine being
observed. `src/kvasir/progress.py` subclasses both.

### Read the output directory off the runner

`STORMWikiRunner.run()` derives a subdirectory from the topic:

```python
self.article_dir_name = truncate_filename(topic.replace(" ", "_").replace("/", "_"))
self.article_output_dir = os.path.join(self.args.output_dir, self.article_dir_name)
```

`truncate_filename` cuts at 125 characters. Read `runner.article_output_dir` after `run()` returns
rather than reimplementing that rule. It is a plain attribute and it survives the call.

`run()` is synchronous, IO-bound, and takes minutes to tens of minutes. Never call it on the event
loop.

### `url_to_info.json` holds two maps, not a list of sources

```json
{
  "url_to_unified_index": { "https://example.org/a": 1 },
  "url_to_info": {
    "https://example.org/a": {
      "url": "...",
      "description": "...",
      "snippets": ["..."],
      "title": "...",
      "meta": {},
      "citation_uuid": -1
    }
  }
}
```

The number behind an article's `[n]` marker comes from `url_to_unified_index`. The `citation_uuid`
field inside a source is a separate per-stage counter, written as `-1` in this file, so reading it
instead would number every citation `-1`.

Polishing rewrites `storm_gen_article_polished.txt` but not this file, so the numbering is the
draft's. That is only safe while `remove_duplicate` stays at its default of `False`, since that is
the flag that renumbers sources.

`tests/fixtures/storm_output/` holds a checked-in output directory in this format.
`test_fixture_matches_what_upstream_actually_writes` loads it with `StormArticle.from_string` and
dumps it again through the tree's own serialiser, so the fixture cannot drift from what STORM
really writes.

### `SearXNG` reads no environment variable

```python
class SearXNG(dspy.Retrieve):
    def __init__(self, searxng_api_url, searxng_api_key=None, k=3, is_valid_source: Callable = None)
```

It raises `RuntimeError("You must supply searxng_api_url")` when the URL is empty. The instance must
have the JSON output format enabled; one serving HTML only returns an empty result set rather than
failing.

`SearXNG` takes its snippets from each result's `content` field and fetches no pages, so it never
reaches `WebPageHelper`. The three-level nesting described above applies to the other retrievers in
the tree.

### `dspy_ai` is pinned at 2.4.9

That is what the tree was written against, and dspy's API changed substantially afterwards. Raising
it is a project, not a dependency bump. `renovate.json` disables the bump.

## Rebasing onto a later upstream

Fetch upstream, diff `fb951af` against the new revision, and apply what it touches by hand. Every
change above is either a fix upstream may have made independently, in which case take theirs, or a
deliberate divergence, in which case reapply it. `runtime.py`, `logs.py`-driven context propagation
and the usage sink have no upstream counterpart and will conflict with nothing.

Verify a rebase with the repository's own checks:

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

`tests/test_storm_correctness.py`, `tests/test_storm_concurrency.py`, `tests/test_storm_runtime.py`
and `tests/test_storm_logging.py` each pin a specific divergence from this page. A rebase that
reverts one of these fixes fails there rather than silently in a run.
