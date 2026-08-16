# kvasir

A self-hostable HTTP service for Stanford STORM and Co-STORM, with an Open WebUI client.

[STORM](https://github.com/stanford-oval/storm) turns a topic into a Wikipedia-style article with
citations by simulating multi-perspective research conversations. Co-STORM is its interactive
sibling, a turn-based round table between you and several agents that accumulates a shared mind
map.

Upstream ships a Python library and a Streamlit demo labelled for local development, but no server
and no container image. This repository supplies the missing middle: a small FastAPI service, a
`linux/amd64` image on GHCR, and two Open WebUI Pipe functions.

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

### Runtime requirements

The image runs as uid 65532 under a read-only root filesystem. Only `/data` and `/tmp` need to be
writable. `/tmp` is required, not optional: `knowledge_storm` opens a cache under `$HOME` while
being imported, and the image points `HOME` there.

## Research a topic

```bash
curl -N -X POST localhost:8080/v1/research \
  -H 'content-type: application/json' \
  -d '{"topic": "The Antikythera mechanism"}'
```

The response is `text/event-stream`:

```
event: progress
data: {"stage": "research", "detail": "completed conversation turn 3"}

event: done
data: {"article": "...", "outline": "...", "citations": [...], "duration_seconds": 412}
```

Stages are `research`, `outline`, `article` and `polish`. A run takes minutes to tens of minutes,
so use a client that streams, and set a generous timeout.

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
| `KVASIR_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model. Co-STORM only. |
| `KVASIR_DATA_DIR` | `/data` | Writable directory for sessions. |
| `KVASIR_SESSION_TTL_HOURS` | `168` | Session expiry, applied at startup. |
| `KVASIR_MAX_CONCURRENT_RUNS` | `1` | Concurrent runs. Beyond this, requests get 429. |
| `KVASIR_SEARCH_TOP_K` | `3` | Search results per query. |
| `KVASIR_MAX_CONV_TURN` | `3` | Questions per perspective. |
| `KVASIR_MAX_PERSPECTIVE` | `3` | Perspectives to research. |
| `LOG_LEVEL` | `INFO` | Standard logging level. |

Model names reach the gateway exactly as written, including any routing prefix such as
`openai/ollama/model:cloud`. Nothing validates, normalises or strips them.

`OPENAI_API_KEY` and `OPENAI_API_BASE` keep those names because litellm and `knowledge_storm`'s
`Encoder` read them directly. There is deliberately no `KVASIR_` alias, since an alias is how
embeddings silently end up on `api.openai.com`.

Saturation returns 429 rather than queueing, because a queued run would outlast any sensible client
timeout.

## Cost and duration

**Not measured.** No gateway was reachable while this was built, so no honest figure for tokens or
wall-clock time on a default run can be given here. The defaults come from upstream's own examples
rather than from measurement. Measure once against your own gateway before deciding whether they
suit you, and note that `max_conv_turn` and `max_perspective` multiply: the research stage is
roughly `max_perspective * max_conv_turn` conversations.

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

[`docs/upstream-notes.md`](docs/upstream-notes.md) records what was read from the installed
`knowledge-storm`, including several traps that produce wrong behaviour rather than errors. Read it
before changing anything that touches upstream.

`knowledge-storm` is dormant: version 1.1.1, last released 2025-01-23. Its pin is expected to sit
still, and a stale pin is not a bug. Renovate is configured accordingly.

## Licence

Dual MIT and Apache-2.0, at your option. See [LICENSE-MIT](LICENSE-MIT) and
[LICENSE-APACHE](LICENSE-APACHE).

STORM and Co-STORM are the work of [Stanford OVAL](https://github.com/stanford-oval/storm), used
here as a published dependency under the MIT licence. This repository neither forks nor vendors it.
