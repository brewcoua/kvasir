# Settings in the database: plan

Status: written 2026-11-05. Phases 1 and 2 built 2026-11-05.

Configuration is environment variables over an optional `kvasir.toml`, read once at startup into a
frozen `Settings`. Every new option means a new key in the file, the README and the example file,
and the web UI's own list of per-run options. This plan keeps only technical values and secrets in
the environment, stores everything else in `kvasir.db`, and edits it from the API and the UI. The
TOML file goes.

## Layers

| Layer | Holds | Edited |
|---|---|---|
| Environment | `KVASIR_TOKEN`, `KVASIR_DATA_DIR`, `KVASIR_HOST`, `KVASIR_PORT`, `KVASIR_LOG_LEVEL`, `KVASIR_LOG_FORMAT`, `KVASIR_MAX_THREADS`, `KVASIR_PROVIDER_<NAME>_API_BASE`, `KVASIR_PROVIDER_<NAME>_API_KEY` | At deploy, read at startup |
| `kvasir.db`, table `settings` | Every other field of `Settings`: models, search, research, storm, fetch, favicons, sources, runs, sessions | `PATCH /v1/settings`, the UI |
| The browser's localStorage | Per-run overrides | The UI's run settings, unchanged |

Each setting has one source. A field stored in the database has no environment variable. The
`settings` table holds one row per field changed from its default, keyed by its path
(`research.max_searches`) with a JSON value. A missing row means the dataclass default, so adding an
option stays one field.

Providers stay in the environment, keys and bases both. An API that could change a provider's
`api_base` would let anyone who can reach it send the provider's key to a host of their choosing.

`kvasir.db` is always created. The `runs`, `calls` and `searches` tables are created only when
`runs.store` is `sqlite`.

## Phases

1. Auth. `KVASIR_TOKEN` is required and startup fails without it. Every route needs it except
   `/healthz`, `/readyz`, `POST /v1/login`, the page and its static assets. A client sends
   `Authorization: Bearer <token>`. The web UI cannot: `EventSource` and `<img>` send no header. So
   `POST /v1/login` with `{"token": ...}` sets an HttpOnly, SameSite=Strict cookie that the API
   accepts too. The UI asks for the token on a 401. The Open WebUI pipe gets a `KVASIR_TOKEN` valve.
2. Storage. The `settings` table, loading from the database and the environment, the TOML file,
   `KVASIR_CONFIG` and `kvasir.example.toml` removed. The validation in `config.py` stays and is fed
   from rows instead of the file.
3. `GET /v1/settings` returns a schema built from field metadata (section, label, hint, type,
   minimum, choices), each field's value, default and whether it is overridden. `PATCH
   /v1/settings` validates the whole merged `Settings`, writes in one transaction and swaps
   `app.state.settings`; 422 on a bad value. `DELETE /v1/settings/{key}` resets one field. A change
   applies live: the `configure_*` calls run again, favicons and sources are rebuilt, `run_slots` is
   resized.
4. Model discovery. `GET /v1/providers` lists names and reachability, never keys.
   `GET /v1/providers/{name}/models` proxies the provider's list as
   `{id, ref, context_length?, capabilities?}`, cached for a few minutes.
5. UI. Login, a setup wizard, and service settings rendered from the schema, with model fields as a
   dropdown of every provider's models.
6. README: the configuration section rewritten.

## First start

With `model.fast`, `model.strong`, `model.embedding` or `search.url` unset, the service starts
anyway. `/readyz` answers 503 with `"setup": "required"`, run endpoints answer 409, and the UI opens
the setup wizard: pick a provider, its models load, `embedding` is suggested from the `embedding`
capability or an id containing `embed`. `fast` and `strong` are the user's pick, since nothing in a
model list says which is which. Then `search.url`, with a reachability check, then save.

## Open questions

What each provider reports beyond an id is unverified:

| Provider | Where | Expected |
|---|---|---|
| Any OpenAI-compatible | `GET {api_base}/models` | `id` only |
| Ollama | `POST /api/show` per model | `capabilities`, `model_info["<arch>.context_length"]` |
| OpenRouter | `GET /models` | `context_length` |
| LiteLLM proxy | `GET /model/info` | `max_input_tokens`, `mode` |

`model.context_tokens` must be the window the server serves, such as Ollama's `num_ctx`, which is
often far below the model's maximum. A reported `context_length` is shown as a hint and never
filled in, since overstating the window truncates prompts silently.
