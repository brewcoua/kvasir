# kvasir

A self-hostable HTTP service for Stanford STORM and Co-STORM, with an Open WebUI client.

[STORM](https://github.com/stanford-oval/storm) turns a topic into a Wikipedia-style article with
citations by simulating multi-perspective research conversations. Co-STORM is its interactive
sibling, a turn-based round table between you and several agents that accumulates a shared mind
map.

Alongside them, kvasir runs its own deep research pipeline, built for a small local model: it
answers a question with a report whose every citation points to a quote read on the page. See
[Research a question](#research-a-question).

Upstream ships a Python library and a Streamlit demo labelled for local development, but no server
and no container image. This repository supplies the missing middle: a small FastAPI service, a
`linux/amd64` image on GHCR, and an Open WebUI Pipe function.

`knowledge_storm` is vendored as `src/kvasir/storm` and modified, rather than depended on. See
[`docs/fork-notes.md`](docs/fork-notes.md) for what diverges and why.

## What it needs

- An OpenAI-compatible gateway. All model and embedding traffic goes there, and nothing calls
  `api.openai.com` directly.
- A [SearXNG](https://docs.searxng.org/) instance **with the JSON output format enabled**. One
  serving HTML only fails as an empty result set rather than an error, which looks like a topic
  with no sources. Add `json` to `search.formats` in its `settings.yml`.
- A writable data directory, for Co-STORM sessions.

No paid search credential is needed, and no database, cache or queue.

## Quickstart

Run kvasir from a checkout with [uv](https://docs.astral.sh/uv/), against a local
[Ollama](https://ollama.com/) and a throwaway SearXNG. This needs uv, Ollama, and podman or docker
for SearXNG only.

1. Pull the models the example configuration names:

   ```bash
   ollama pull qwen3.8:27b
   ollama pull nomic-embed-text
   ```

   Ollama loads a model with a 4096-token context by default, which is too small for the article
   and polish prompts. Start it with a larger one, for example `OLLAMA_CONTEXT_LENGTH=16384 ollama
   serve`, or set that variable in its service unit. A larger context takes more memory, so more of
   the model may run on the CPU.

2. Start SearXNG with JSON output enabled, from the repository root:

   ```bash
   podman run -d --name searxng -p 127.0.0.1:8888:8080 \
     -e SEARXNG_SECRET="$(openssl rand -hex 32)" -e FORCE_OWNERSHIP=false \
     -v ./searxng/settings.yml:/etc/searxng/settings.yml:ro,Z \
     docker.io/searxng/searxng:latest
   ```

   `docker` takes the same arguments. [`searxng/settings.yml`](searxng/settings.yml) only adds
   `json` to the default formats. Check it answers in JSON:

   ```bash
   curl -s 'localhost:8888/search?q=test&format=json' | head -c 100
   ```

   Remove it with `podman rm -f searxng`.

3. Configure and start kvasir:

   ```bash
   cp kvasir.example.toml kvasir.toml
   uv run python -m kvasir
   ```

   Edit `kvasir.toml` to use other models. See [Configuration](#configuration) for every key.

4. Check both dependencies are reachable:

   ```console
   $ curl -s localhost:8080/readyz
   {"gateway":true,"searxng":true}
   ```

Then [write an article](#write-an-article). On one local model a run takes much longer than through
a hosted gateway.

kvasir binds `0.0.0.0:8080` and has no authentication, so on a laptop it is reachable from the local
network.

## Run it

```bash
podman run --rm \
  --read-only --tmpfs /tmp --user 65532:65532 \
  -p 8080:8080 -v kvasir-data:/data \
  -e OPENAI_API_KEY=... \
  -e OPENAI_API_BASE=https://gateway.example/v1 \
  -e KVASIR_MODEL_FAST=openai/ollama/fast-model:cloud \
  -e KVASIR_MODEL_STRONG=openai/ollama/strong-model:cloud \
  -e KVASIR_SEARXNG_URL=http://searxng.example \
  ghcr.io/brewcoua/kvasir:0.1.0
```

Verify it started:

```console
$ curl -s localhost:8080/healthz
{"status":"ok"}
$ curl -s localhost:8080/readyz
{"gateway":true,"searxng":true}
```

`/readyz` returns 503 if either dependency is unreachable, and names which one. A missing or
unusable variable makes the container exit at startup listing every variable at fault, rather than
starting and failing on the first request.

Pin the digest in production. `ghcr.io/brewcoua/kvasir:0.1.0@sha256:...` is the intended form; the
publishing workflow writes the digest to its run summary. There is deliberately no moving `latest`.

Open `localhost:8080/` for the runs page: what is running now, which stage it is in, how long each
stage took, and what it has spent.

### Runtime requirements

The image runs as uid 65532 under a read-only root filesystem. Only `/data` and `/tmp` need to be
writable. `/tmp` is required, not optional: dspy creates a disk cache directory while it is being
imported, whether or not caching is on, and `src/kvasir/storm/__init__.py` points `DSPY_CACHEDIR`
there. A run's scratch directory also lives under `/tmp`.

## Research a question

`POST /v1/research` answers a question with a cited report. It searches in rounds, reads each page in
pieces small enough for a 16k context, keeps only claims whose quote appears word for word on the
page, and writes the report one section at a time from those claims. See
[`docs/deep-research-plan.md`](docs/deep-research-plan.md) for the design.

1. Optionally, ask what the question leaves open. This is one fast-model call, like `/v1/tasks`: it
   registers no run and takes no slot.

   ```bash
   curl -s -X POST localhost:8080/v1/research/clarify \
     -H 'content-type: application/json' \
     -d '{"question": "Which open-weight LLMs are best for local coding?"}'
   ```

   The answer is `{"questions": [...]}`, at most three, and empty when the question is clear.

2. Start the run, with any answers:

   ```bash
   curl -N -X POST localhost:8080/v1/research \
     -H 'content-type: application/json' \
     -d '{"question": "Which open-weight LLMs are best for local coding?",
          "clarifications": [{"question": "What hardware?", "answer": "One 24 GB GPU"}]}'
   ```

   It streams like an article run. Stages are `brief`, `research` and `write`. The `done` frame
   carries `report` (markdown, with `[n]` markers), `assumptions` (what the brief decided on your
   behalf), `citations`, `claims` (every claim gathered, each with its quote and source) and
   `duration_seconds`.

Optional fields: `max_rounds` (default 4), `queries_per_round` (4), `results_per_query` (3),
`model_fast`, `model_strong`. A round that adds fewer than 5 claims ends the research early. A run
that finds no claim at all says so in `report` and writes no sections.

## Write an article

```bash
curl -N -X POST localhost:8080/v1/article \
  -H 'content-type: application/json' \
  -d '{"topic": "The Antikythera mechanism"}'
```

The response is `text/event-stream`:

```
event: run
data: {"run_id": "0f3c9a1b7e42"}

event: progress
data: {"stage": "research", "detail": "completed conversation turn 3", "at": 1793475466.3, "done": 3, "total": 12}

event: done
data: {"article": "...", "outline": "...", "citations": [...], "duration_seconds": 412}
```

Stages are `research`, `outline`, `article` and `polish`. A run takes minutes to tens of minutes,
so use a client that streams, and set a generous timeout.

`at` is the Unix time of the event. `done` and `total` count progress through the stage when it has
a countable unit, and are null otherwise: sections in `article`, conversation turns in `research`.
The research `total` is an upper bound, since a conversation can end early.

Every streaming response opens with a `run` frame. Use its `run_id` to follow the run at
`/v1/runs/{id}` if the client stops reading, and to attribute cost afterwards. A client that does
not know the event name ignores it.

Optional fields: `search_top_k`, `max_conv_turn`, `max_perspective`, `do_polish_article`,
`model_fast`, `model_strong`. Each falls back to the configured default.

## Hold a round table

Sessions are keyed by an id you supply, so a client can key them by its own conversation id.

| Request | Effect |
| --- | --- |
| `POST /v1/session` | Create and warm start. Streams. Body: `session_id`, `topic`. 409 if the id is taken. |
| `POST /v1/session/{id}/step` | One turn. Body `{"utterance": "..."}` speaks, `{}` advances the round table. Streams. |
| `POST /v1/session/{id}/report` | The report so far, with citations. Streams. |
| `GET /v1/session/{id}` | Topic, turn count and experts. |
| `DELETE /v1/session/{id}` | Remove it. |

An utterance is recorded rather than answered, so getting a reply to something you said takes a
second step with an empty body. The Pipe does this for you.

Sessions are one JSON file each under `$KVASIR_DATA_DIR/sessions`, written to a temporary sibling
and renamed, so a crash mid-write leaves the previous session readable. Expired sessions are swept
once at startup, with no scheduler and no background task.

## Watch what is running

`GET /` serves a single page that starts runs and lists them. To start one, pick research or
article, enter the question or topic, and press Start. For research, the page asks the clarifying
questions first; answer what you can, or skip. For a selected run it shows elapsed time, totals for
tokens in and out, reasoning tokens, cost, searches and embedding tokens, then the stages on a
timeline with each stage's duration and usage, the most recent model calls with a clock on any still
waiting, the usage split by role and by model, the result once
the run finishes, and the event log. Figures update live while the run is in progress. A run with a
result has an icon in the list that opens it directly. The result opens over the whole page, with
each `[n]` shown as a badge naming the source's site; hovering or focusing one shows its title, URL
and quote. The theme selector in the header offers `system`, `light`, `dark`, `paper`, `riso`,
`amber`, `moss` and `navy`, and the choice is kept in the browser. It is one self-contained file
with no external asset, since the image is read-only and offline.

The same data is available as JSON.

| Request | Effect |
| --- | --- |
| `GET /v1/runs` | Every run kept, newest first. `has_result` says whether its detail carries a result. |
| `GET /v1/runs/{id}` | One run, with per-stage timings, usage, its last 50 model calls, recent events and its result. |
| `GET /v1/runs/{id}/events` | Follow a run live. Streams. Opens and closes with a `snapshot`, and sends a `status` after each `progress` and every 2 seconds in between. |
| `POST /v1/tasks` | One short completion on the fast model. Body `{"messages": [...]}`, answers `{"content": "..."}`. |

`POST /v1/tasks` is the one route that is not a run: it registers nothing and takes no slot. It
exists because a chat client generates a title, tags and follow-up prompts by asking whichever
model the chat uses, and those must not queue behind — or displace — the research they describe.

A run is `queued`, `running`, `done`, `failed` or `rejected`. `rejected` means it never got a slot,
so a 429 is visible as a run rather than only as a status code.

Usage is split three ways: `by_model`, `by_role` and `by_stage`. A call counts toward the stage the
run was in when the call finished, and calls made before the first progress event count as
`unstaged`. Co-STORM can change stage while a call is in flight, so its stage split is approximate.
`reasoning_tokens` is part of `completion_tokens`, not added to it, and stays zero when the gateway
does not report it. A stage entered more than once has one span per entry, and its `seconds` sums
them.

Cost comes from what the call reports. Three shapes are read, in order: the cost dspy itself
computes for a model it recognises, a LiteLLM proxy's `x-litellm-response-cost` response header, and
a `cost` field in the response's `usage` object, either a number or split into `prompt_cost` and
`completion_cost` as Bifrost reports it. A gateway that reports no cost, for a model dspy does not
price, leaves it at zero, which is not the same as free.

Runs are held in memory, capped at the `KVASIR_MAX_RUNS` most recent, and by default do not survive
a restart. With `KVASIR_RUN_STORE=sqlite` they are also written to `$KVASIR_DATA_DIR/kvasir.db`,
once when they start and once when they end, and the list and detail routes read from it as well.
A run still `queued` or `running` there at startup was cut short by the restart, and is marked
`failed` with the error `interrupted by a restart`. Retention is applied at startup and after each
run ends: older than `KVASIR_RUN_TTL_DAYS`, or beyond the `KVASIR_MAX_RUNS` most recent, is deleted.
Only a run in this process's memory can be followed live; following a stored one sends its snapshot
and ends. Memory is the default because a client such as Open WebUI keeps its own record of every
answer, so the store is for debugging the service.

## Configuration

Everything is an environment variable, or a key in a TOML file. The first five are required and the
service will not start without them.

The file is `$KVASIR_CONFIG`, or `kvasir.toml` in the working directory when that is unset. Its keys
are the variable names in lower case without `KVASIR_`, so `KVASIR_MODEL_FAST` is `model_fast` and
`OPENAI_API_BASE` is `openai_api_base`. A variable that is set wins over the file. An unknown key, or
a `KVASIR_CONFIG` that cannot be read, stops startup. [`kvasir.example.toml`](kvasir.example.toml)
is a complete file for a local Ollama.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | required | Gateway credential. |
| `OPENAI_API_BASE` | required | Gateway base URL. |
| `KVASIR_MODEL_FAST` | required | Conversation simulation, question asking, polishing. |
| `KVASIR_MODEL_STRONG` | required | Outline and article generation. |
| `KVASIR_SEARXNG_URL` | required | SearXNG base URL. |
| `KVASIR_EMBEDDING_MODEL` | `openai/text-embedding-3-small` | Embedding model. Used by both modes. |
| `KVASIR_DATA_DIR` | `/data` | Writable directory for sessions, and for the run database. |
| `KVASIR_SESSION_TTL_HOURS` | `168` | Session expiry, applied at startup. |
| `KVASIR_RUN_STORE` | `memory` | `memory`, or `sqlite` to keep runs across restarts in `$KVASIR_DATA_DIR/kvasir.db`. |
| `KVASIR_MAX_RUNS` | `100` | Runs kept, most recent first, in either store. |
| `KVASIR_RUN_TTL_DAYS` | `30` | Age past which a stored run is deleted. `sqlite` only. |
| `KVASIR_MAX_CONCURRENT_RUNS` | `1` | Concurrent runs. Beyond this, requests get 429. |
| `KVASIR_MAX_THREADS` | `10` | Width of each thread pool, and the cap on outbound search and page requests. |
| `KVASIR_SEARCH_TOP_K` | `3` | Search results per query. |
| `KVASIR_MAX_CONV_TURN` | `3` | Questions per perspective. |
| `KVASIR_MAX_PERSPECTIVE` | `3` | Perspectives to research. |
| `LOG_LEVEL` | `INFO` | Standard logging level. |
| `LOG_FORMAT` | `json` | `json` or `text`. `text` is for reading logs by eye. |
| `KVASIR_REASONING_EFFORT` | unset | Sent as `reasoning_effort` in every completion's body. Unset sends nothing. |
| `KVASIR_CONFIG` | `kvasir.toml` | Config file. Environment only. |

Set `KVASIR_REASONING_EFFORT` to `none` for a thinking model on Ollama. Its reasoning counts against
each role's token budget, which is a few hundred tokens for the outline and article roles, so the
answer is cut off before it starts. It goes in the request body rather than through litellm's own
parameter, which litellm refuses for a model it does not know.

`KVASIR_MODEL_FAST` and `KVASIR_MODEL_STRONG` must name a provider first: dspy routes on the leading
`/`-separated segment and consumes it, and everything after it reaches the gateway untouched. For an
OpenAI-compatible gateway that means an `openai/` prefix, so `openai/ollama/fast-model:cloud` asks
the gateway for `ollama/fast-model:cloud`. A name without a prefix is rejected at startup rather
than silently prefixed. `KVASIR_EMBEDDING_MODEL` works the same way, since embeddings go through
dspy too.

Each log line is one JSON object carrying `run_id`, `run_kind` and `stage`, including lines from
inside the pipeline's thread pools. That is what makes concurrent runs separable in a log.

Every line, including uvicorn's access log. Start the service as `python -m kvasir` rather than
`uvicorn kvasir.main:app` to get that: the module configures logging first and tells uvicorn to
install none of its own, which is what stops the access log printing as plain text. The image does
this already.

`OPENAI_API_KEY` and `OPENAI_API_BASE` keep those names because they are the conventional spelling
for an OpenAI-compatible endpoint, which is what the gateway serves. Both are passed explicitly to
every model and to the encoder; neither is exported back into the environment, so there is no path
by which a default reaches `api.openai.com`.

Saturation returns 429 rather than queueing, because a queued run would outlast any sensible client
timeout.

## Cost and duration

**Not measured here.** No gateway was reachable while this was built, so no honest figure for
tokens or wall-clock time on a default run can be given. The defaults come from upstream's own
examples rather than from measurement.

Measure against your own gateway. `GET /v1/runs/{id}` reports a finished run's tokens and cost split
by stage, by role and by model, which is enough to decide whether the defaults suit you. Note that
`max_conv_turn` and `max_perspective` multiply: the research stage is roughly
`max_perspective * max_conv_turn` conversations.

## Open WebUI

One Pipe function lives under [`owui/`](owui/), exposing STORM and Co-STORM as two
models. It writes a run's progress into the message as it happens, turns sources into citations,
and reports what the run spent. Open WebUI's own title, tag and follow-up generation is routed to
`POST /v1/tasks`, so naming a chat does not cost a run. Installation instructions and a valve
reference are in [`owui/README.md`](owui/README.md).

## Development

```bash
uv sync
uv run pytest              # no network, no credentials
uv run ruff check .
uv run mypy
uv run pytest -m integration   # needs a real gateway and SearXNG
```

[`docs/fork-notes.md`](docs/fork-notes.md) records every divergence from upstream, what it fixes,
and how to rebase onto a later upstream. Read it before changing anything under `src/kvasir/storm`.

That directory is excluded from `ruff format --check`, from the strict lint rules and from `mypy`,
until the unused retrievers and deprecated model wrappers are trimmed. Trimming them is a separate
pass.

`dspy` is pinned at 3.3.0. The fork was written against 2.4.9, whose template engine is gone: the
signatures now carry types and descriptions rather than prompt prefixes, and dspy's adapters turn
them into prompts and parse the answers back. Where upstream picked structure out of prose with
regexes, the output field states its type instead.

## Licence

Dual MIT and Apache-2.0, at your option. See [LICENSE-MIT](LICENSE-MIT) and
[LICENSE-APACHE](LICENSE-APACHE).

STORM and Co-STORM are the work of [Stanford OVAL](https://github.com/stanford-oval/storm). Their
`knowledge_storm` package is vendored and modified under `src/kvasir/storm`, which stays MIT only,
as upstream is. See [`src/kvasir/storm/NOTICE`](src/kvasir/storm/NOTICE) for the fork point and
[`docs/fork-notes.md`](docs/fork-notes.md) for what was changed.
