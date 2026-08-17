# kvasir

A self-hostable HTTP service for Stanford STORM and Co-STORM, with an Open WebUI client.

[STORM](https://github.com/stanford-oval/storm) turns a topic into a Wikipedia-style article with
citations by simulating multi-perspective research conversations. Co-STORM is its interactive
sibling, a turn-based round table between you and several agents that accumulates a shared mind
map.

Upstream ships a Python library and a Streamlit demo labelled for local development, but no server
and no container image. This repository supplies the missing middle: a small FastAPI service, a
`linux/amd64` image on GHCR, and two Open WebUI Pipe functions.

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

## Research a topic

```bash
curl -N -X POST localhost:8080/v1/research \
  -H 'content-type: application/json' \
  -d '{"topic": "The Antikythera mechanism"}'
```

The response is `text/event-stream`:

```
event: run
data: {"run_id": "0f3c9a1b7e42"}

event: progress
data: {"stage": "research", "detail": "completed conversation turn 3"}

event: done
data: {"article": "...", "outline": "...", "citations": [...], "duration_seconds": 412}
```

Stages are `research`, `outline`, `article` and `polish`. A run takes minutes to tens of minutes,
so use a client that streams, and set a generous timeout.

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
| `POST /v1/session/{id}/report` | The report so far, with citations. |
| `GET /v1/session/{id}` | Topic, turn count and experts. |
| `DELETE /v1/session/{id}` | Remove it. |

An utterance is recorded rather than answered, so getting a reply to something you said takes a
second step with an empty body. Both Pipes do this for you.

Sessions are one JSON file each under `$KVASIR_DATA_DIR/sessions`, written to a temporary sibling
and renamed, so a crash mid-write leaves the previous session readable. Expired sessions are swept
once at startup, with no scheduler and no background task.

## Watch what is running

`GET /` serves a single page listing runs, and for a selected run its stages with timings, its token
and cost tally split by role and by model, and its event log. It is one self-contained file with no
external asset, since the image is read-only and offline.

The same data is available as JSON.

| Request | Effect |
| --- | --- |
| `GET /v1/runs` | Every run the process remembers, newest first. |
| `GET /v1/runs/{id}` | One run, with per-stage timings, usage and recent events. |
| `GET /v1/runs/{id}/events` | Follow a run live. Streams. Opens and closes with a snapshot. |

A run is `queued`, `running`, `done`, `failed` or `rejected`. `rejected` means it never got a slot,
so a 429 is visible as a run rather than only as a status code.

Cost comes from what the call reports. Three shapes are read, in order: the cost dspy itself
computes for a model it recognises, a LiteLLM proxy's `x-litellm-response-cost` response header, and
a `cost` field in the response's `usage` object, either a number or split into `prompt_cost` and
`completion_cost` as Bifrost reports it. A gateway that reports no cost, for a model dspy does not
price, leaves it at zero, which is not the same as free. Runs are held in memory, capped at the 100 most recent, and do not
survive a restart.

## Configuration

Everything is environment variables. The first five are required and the service will not start
without them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | required | Gateway credential. |
| `OPENAI_API_BASE` | required | Gateway base URL. |
| `KVASIR_MODEL_FAST` | required | Conversation simulation, question asking, polishing. |
| `KVASIR_MODEL_STRONG` | required | Outline and article generation. |
| `KVASIR_SEARXNG_URL` | required | SearXNG base URL. |
| `KVASIR_EMBEDDING_MODEL` | `openai/text-embedding-3-small` | Embedding model. Used by both modes. |
| `KVASIR_DATA_DIR` | `/data` | Writable directory for sessions. |
| `KVASIR_SESSION_TTL_HOURS` | `168` | Session expiry, applied at startup. |
| `KVASIR_MAX_CONCURRENT_RUNS` | `1` | Concurrent runs. Beyond this, requests get 429. |
| `KVASIR_MAX_THREADS` | `10` | Width of each thread pool, and the cap on outbound search and page requests. |
| `KVASIR_SEARCH_TOP_K` | `3` | Search results per query. |
| `KVASIR_MAX_CONV_TURN` | `3` | Questions per perspective. |
| `KVASIR_MAX_PERSPECTIVE` | `3` | Perspectives to research. |
| `LOG_LEVEL` | `INFO` | Standard logging level. |
| `LOG_FORMAT` | `json` | `json` or `text`. `text` is for reading logs by eye. |

`KVASIR_MODEL_FAST` and `KVASIR_MODEL_STRONG` must name a provider first: dspy routes on the leading
`/`-separated segment and consumes it, and everything after it reaches the gateway untouched. For an
OpenAI-compatible gateway that means an `openai/` prefix, so `openai/ollama/fast-model:cloud` asks
the gateway for `ollama/fast-model:cloud`. A name without a prefix is rejected at startup rather
than silently prefixed. `KVASIR_EMBEDDING_MODEL` works the same way, since embeddings go through
dspy too.

Each log line is one JSON object carrying `run_id`, `run_kind` and `stage`, including lines from
inside the pipeline's thread pools. That is what makes concurrent runs separable in a log.

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
by role and by model, which is enough to decide whether the defaults suit you. Note that
`max_conv_turn` and `max_perspective` multiply: the research stage is roughly
`max_perspective * max_conv_turn` conversations.

## Open WebUI

Two Pipe functions live under [`openwebui/`](openwebui/), with installation instructions and a
valve reference in [`openwebui/README.md`](openwebui/README.md).

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
