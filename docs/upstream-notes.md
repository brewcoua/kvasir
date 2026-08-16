# Upstream notes: knowledge-storm 1.1.1

Facts read from the installed `knowledge-storm==1.1.1`, not from upstream documentation. Upstream
is dormant, so treat this API as frozen. Re-verify this page if the pin ever moves.

Paths below are relative to the installed `knowledge_storm` package.

## Encoder redirection works, and Co-STORM is in scope

The service must never call `api.openai.com`. Co-STORM needs embeddings, and it builds its own
`Encoder()` internally with no arguments, so nothing can be passed in by the caller. `Encoder`
accepts an `api_base` argument but drops it for `encoder_type="openai"`, keeping only the API key
(`encoder.py`):

```python
if encoder_type.lower() == "openai":
    self.embedding_model_name = "text-embedding-3-small"
    self.kargs = {"api_key": api_key or os.getenv("OPENAI_API_KEY")}
```

Redirection therefore depends on litellm reading `OPENAI_API_BASE` from the environment.

`scripts/probe_encoder.py` confirms it does. The probe stands up an HTTP server on loopback, points
`OPENAI_API_BASE` at it, and asks the `Encoder` for one embedding. The request arrives:

```
gateway stand-in: http://127.0.0.1:33333/v1
request path:     /v1/embeddings
request model:    text-embedding-3-small
vector returned:  [0.1 0.1 0.1 0.1]
VERDICT: HONOURED, litellm used OPENAI_API_BASE. Co-STORM is in scope.
```

Both modes are buildable. Run `uv run python scripts/probe_encoder.py` to reproduce; it needs no
credentials and no network.

### The embedding model name is hardcoded, and must be overwritten

The name sent is always `text-embedding-3-small`. A gateway that routes on a prefix will not have a
model under that bare name, so it has to be replaced. No subclass is needed:
`CoStormRunner.__init__` stores the encoder on `self.encoder` and passes that same instance to its
sub-components, so assigning the attribute after construction reaches all of them.

`CoStormRunner.from_dict` builds a **fresh** `Encoder()` at `collaborative_storm/engine.py:565`.
Reapply the override after every deserialisation, not only after construction.

### `ENCODER_API_TYPE` is required

`Encoder.__init__` raises `ValueError("ENCODER_API_TYPE environment variable is not set.")` when it
is missing. Set it to `openai`.

### Importing `encoder.py` writes to `$HOME`

At import time, before any class is instantiated:

```python
disk_cache_dir = os.path.join(Path.home(), ".storm_local_cache")
litellm.cache = Cache(disk_cache_dir=disk_cache_dir, type="disk")
```

Under a read-only root filesystem this fails at import. `HOME` must point somewhere writable before
`knowledge_storm` is imported.

### Embedding failures are swallowed

`Encoder._get_text_embeddings` catches per-item exceptions, prints them, and continues. A partly
failed batch returns fewer vectors than it was given texts, with no exception raised. Treat a short
result as an error.

## Language models: `LitellmModel`

`OpenAIModel` is deprecated and takes no `api_base`. `LitellmModel` (`lm.py`) merges
`{**self.kwargs, **kwargs}` into `litellm.completion()`, so `api_base` reaches litellm:

```python
def __init__(self, model: str = "openai/gpt-4o-mini", api_key: Optional[str] = None,
             model_type: Literal["chat", "text"] = "chat", **kwargs)
```

`CollaborativeStormLMConfigs.init()` hardcodes `"api_base": None` and cannot be pointed at a
gateway. Do not call it. Set all six Co-STORM roles and all five STORM roles explicitly.

STORM roles (`STORMWikiLMConfigs`): `set_conv_simulator_lm`, `set_question_asker_lm`,
`set_outline_gen_lm`, `set_article_gen_lm`, `set_article_polish_lm`.

Co-STORM roles (`CollaborativeStormLMConfigs`): `set_question_answering_lm`,
`set_discourse_manage_lm`, `set_utterance_polishing_lm`, `set_warmstart_outline_gen_lm`,
`set_question_asking_lm`, `set_knowledge_base_lm`.

## Retrieval: `SearXNG`

```python
class SearXNG(dspy.Retrieve):
    def __init__(self, searxng_api_url, searxng_api_key=None, k=3, is_valid_source: Callable = None)
```

Reads no environment variable, and raises `RuntimeError("You must supply searxng_api_url")` when the
URL is empty. `CoStormRunner` defaults `rm` to `BingSearch`, which needs a paid key, so always pass
the retriever explicitly.

The target SearXNG must have the JSON output format enabled. One serving HTML only fails as an
empty result set rather than an error.

## STORM writes to disk and returns nothing

`STORMWikiRunnerArguments`: `output_dir` (required), `max_conv_turn=3`, `max_perspective=3`,
`max_search_queries_per_turn=3`, `disable_perspective=False`, `search_top_k=3`, `retrieve_top_k=3`,
`max_thread_num=10`.

`run()` writes `conversation_log.json`, `raw_search_results.json`, `direct_gen_outline.txt`,
`storm_gen_outline.txt`, `storm_gen_article.txt`, `storm_gen_article_polished.txt`,
`url_to_info.json`. `post_run()` adds `run_config.json` and `llm_call_history.jsonl`.

### Read the output directory off the runner, do not recompute it

`run()` derives a subdirectory from the topic (`storm_wiki/engine.py:379`):

```python
self.article_dir_name = truncate_filename(topic.replace(" ", "_").replace("/", "_"))
self.article_output_dir = os.path.join(self.args.output_dir, self.article_dir_name)
```

`truncate_filename` cuts at 125 characters. Rather than reimplementing that rule, read
`runner.article_output_dir` after `run()` returns. It is a plain attribute, it survives the call,
and it cannot drift from upstream.

`run()` is synchronous, IO-bound, and takes minutes to tens of minutes, using `max_thread_num`
threads internally. Never call it on the event loop.

### Plain STORM needs a local embedding model too

Article generation calls `information_table.prepare_table_for_retrieval()`
(`storm_wiki/modules/article_generation.py:70`), which runs:

```python
self.encoder = SentenceTransformer("paraphrase-MiniLM-L6-v2")
```

This is a local model fetched from HuggingFace, unrelated to the gateway and unrelated to the
`Encoder` that Co-STORM uses. It is why `sentence-transformers` and `torch` cannot be dropped from
the dependency tree.

A run therefore reaches out to huggingface.co the first time unless the model is already cached.
The image must ship the weights and point `HF_HOME` at them, or article generation fails on a
network-restricted or read-only deployment.

### `run()` accepts one stage at a time, and reloads state from disk

Each stage has a `do_*` flag, and a stage with a disabled predecessor reloads what the previous one
wrote (`_load_information_table_from_local_fs`, `_load_outline_from_local_fs`,
`_load_draft_article_from_local_fs`). Calling `run()` once per stage is therefore supported and
costs only some JSON parsing, which is how the service gets exact stage boundaries despite the
missing callbacks below.

### There are two unrelated `BaseCallbackHandler` classes

`knowledge_storm.storm_wiki.modules.callback.BaseCallbackHandler` and
`knowledge_storm.collaborative_storm.modules.callback.BaseCallbackHandler` share a name and nothing
else. They have different methods and neither inherits from the other. Import the one matching the
engine being observed.

STORM's has eight methods, all in the research and outline stages:
`on_identify_perspective_start`, `on_identify_perspective_end(perspectives)`,
`on_information_gathering_start`, `on_dialogue_turn_end(dlg_turn)`,
`on_information_gathering_end`, `on_information_organization_start`,
`on_direct_outline_generation_end(outline)`, `on_outline_refinement_end(outline)`.

**Nothing reports article generation or polishing.** The last callback of a run is
`on_outline_refinement_end`, after which the longest part of the run happens silently. Progress for
those stages has to come from whatever drives the run, not from upstream.

### `url_to_info.json` holds two maps, not a list of sources

`dump_reference_to_file` writes:

```json
{
  "url_to_unified_index": {"https://example.org/a": 1},
  "url_to_info": {"https://example.org/a": {"url": "...", "description": "...",
                                            "snippets": ["..."], "title": "...",
                                            "meta": {}, "citation_uuid": -1}}
}
```

The number behind an article's `[n]` marker comes from `url_to_unified_index`. The `citation_uuid`
field inside a source is a separate per-stage counter, and upstream writes it as `-1` in this file,
so reading it instead would number every citation `-1`.

Polishing rewrites `storm_gen_article_polished.txt` but does not rewrite this file, so the
numbering is the draft's. That is only safe while `remove_duplicate` stays at its default of
`False`, since that is the flag that renumbers sources.

`tests/fixtures/storm_output/` holds a checked-in output directory in this format.
`test_fixture_matches_what_upstream_actually_writes` loads it with `StormArticle.from_string` and
dumps it again through upstream's own serialiser, so the fixture cannot drift from what STORM
really writes.
