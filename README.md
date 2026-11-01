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

How the service fits together, from a request to its result, is in
[`docs/architecture.md`](docs/architecture.md).

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

3. Build the runs page. This needs Node 26 and pnpm:

   ```bash
   (cd web && pnpm install && pnpm build)
   ```

   Without it the API works, and `GET /` answers 503 with the command to run.

4. Configure and start kvasir:

   ```bash
   cp kvasir.example.toml kvasir.toml
   uv run python -m kvasir
   ```

   Edit `kvasir.toml` to use other models. See [Configuration](#configuration) for every key.

5. Check both dependencies are reachable:

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
unusable variable makes the container exit at startup, rather than starting and failing on the
first request. Missing required variables are listed together; an unusable value is reported on its
own, so fixing one may reveal the next.

Pin the digest in production. `ghcr.io/brewcoua/kvasir:0.1.0@sha256:...` is the intended form; the
publishing workflow writes the digest to its run summary. There is deliberately no moving `latest`.

Open `localhost:8080/` for the runs page: what is running now, which stage it is in, how long each
stage took, and what it has spent.

### Runtime requirements

The image runs as uid 65532 under a read-only root filesystem. Only `/data` and `/tmp` need to be
writable. `/tmp` is required, not optional: dspy creates a disk cache directory while it is being
imported, whether or not caching is on, and `src/kvasir/storm/__init__.py` points `DSPY_CACHEDIR`
there. A run's scratch directory also lives under `/tmp`.

## Start a run

Every run starts at `POST /v1/runs`. The body names a method, the input and the method's options:

```json
{"method": "ledger", "input": {"question": "..."}, "options": {"max_searches": 12}}
```

| Method | Produces |
| --- | --- |
| `ledger` | A cited report answering a question, or a ranking or a table comparing options. |
| `graph` | A cited report answering a question, following the questions each finding raises. |
| `storm` | A Wikipedia-style article on the topic given as `input.question`. |

`GET /v1/methods` lists each method with its stages, whether it asks clarifying questions, and its
options as a JSON Schema whose defaults are the configured ones. An option the method does not take
is refused with 422, before any run starts.

## Research a question

The `ledger` method answers a question with a cited report. It splits the brief into questions and
searches for one at a time: a question the search leaves open is searched for again with a narrower
query, and one it answers can add harder follow-ups. It reads each page in pieces small enough for
a 16k context, keeps only claims whose quote appears word for word on the page, and writes the
report one section at a time from those claims. Each sentence that cites a claim is then checked
against the claim's quote, and taken out if the quote does not support it. Up to 4 removed
sentences are searched for again, and a section whose search finds support is written and checked
once more. See [`docs/research.md`](docs/research.md) for how each step works and why.

1. Optionally, ask what the question leaves open. This is one fast-model call, like `/v1/tasks`: it
   registers no run and takes no slot.

   ```bash
   curl -s -X POST localhost:8080/v1/clarify \
     -H 'content-type: application/json' \
     -d '{"question": "Which open-weight LLMs are best for local coding?"}'
   ```

   The answer is `{"questions": [...]}`, at most three, and empty when the question is clear.

2. Start the run, with any answers:

   ```bash
   curl -N -X POST localhost:8080/v1/runs \
     -H 'content-type: application/json' \
     -d '{"method": "ledger",
          "input": {"question": "Which open-weight LLMs are best for local coding?",
                    "clarifications": [{"question": "What hardware?", "answer": "One 24 GB GPU"}]}}'
   ```

   It streams like an article run. Stages are `brief`, `background`, `research`, `assess`,
   `write` and `verify`; `background` and `assess` run only for a ranking or a table. The `done`
   frame carries `report` (markdown, with `[n]` markers), `assumptions` (what the brief decided on
   your behalf), `citations`, `claims` (every claim gathered, each with its quote and source),
   `questions` (each question searched for, with its status, queries and supporting claims),
   `removed` (each sentence verification took out, with its section and the reason), `output`,
   `criteria`, `matrix` and `duration_seconds`.

Options: `output`, `criteria`, `entities`, `max_searches`, `results_per_query`, `max_attempts`,
`follow_ups`, `max_depth`, `max_entities`, `max_criteria`, `frontier`, `ground`, `corroborate`,
`contradict`, `model_fast`, `model_strong`. Each budget left out takes its `KVASIR_RESEARCH_*` default from
[Configuration](#configuration). The research stops when the searches are spent, no question is left
open, or 4 searches in a row add no claim. A question gets `max_attempts` queries, each a different
shape: plain words, the exact terms its answer would contain, a primary source (`site:` or
`filetype:pdf`), then the broader topic. Questions are searched least tried first. With
`"frontier": "worth"` they are ordered by a score from the claims held, and a branch that stops
adding claims is no longer searched, as
[`docs/research.md`](docs/research.md#picking-by-worth) describes. The report ends with an "Open
questions" section listing what the research did not settle. A run that finds no claim at all says
so in `report` and writes no sections.

### Follow what the research raises

The `graph` method answers a question with a cited report, like `ledger`, but explores rather than
works through a list. It searches the question worth most first, and the evidence a search finds
raises child questions under it. Each judgement reads only the question's path from its first
question and the claims found for it. A branch, a first question and every question raised under
it, closes after 2 searches in a row add nothing, or once it has spent `branch_searches` (6 by
default). The report has one section per branch that found something. It never makes a ranking or
a table.

```bash
curl -N -X POST localhost:8080/v1/runs \
  -H 'content-type: application/json' \
  -d '{"method": "graph", "input": {"question": "Why did the Kvasir stone end up in a museum?"}}'
```

Stages are `brief`, `research`, `write` and `verify`, and the `done` frame has the same fields as a
`ledger` run. Options: `max_searches`, `results_per_query`, `max_attempts`, `max_depth`,
`branch_searches`, `ground`, `corroborate`, `contradict`, `model_fast`, `model_strong`. See
[`docs/research.md`](docs/research.md#the-graph-method).

### Check claims more strictly

Three options add checks past the quote check, each at a cost in calls.

| Option | Default | Effect |
| --- | --- | --- |
| `ground` | on when `KVASIR_MODEL_CHECKER` is set | The checker drops each claim the page piece it was read from does not support, and decides each sentence citing one claim. Without a checker it does nothing. |
| `corroborate` | `false` | Each claim is compared with up to 3 close claims from other sites. Those that support it go in its `corroborated_by`. |
| `contradict` | `false` | Close claims from other sites that disagree with it go in its `contradicted_by`, both ways. |

To use MiniCheck on Ollama, pull it, then point the checker at it through the gateway:

```bash
ollama pull bespoke-minicheck
export KVASIR_MODEL_CHECKER=openai/bespoke-minicheck
```

Bespoke-MiniCheck-7B is licensed CC BY-NC 4.0. Whether Ollama keeps it loaded next to the main
model, or swaps the two on each call, depends on its memory; a swap on every check makes a run far
slower.

### Rank or compare options

Ask for it in the question: "Rank the best phones of 2026 on price, performance and privacy". The
brief reads the output type (`ranking`, `table` or `report`), the criteria and the options from the
question. Criteria and options it names are used as given. What it leaves open the run chooses from
a first search: at most `max_entities` options, and up to `max_criteria` criteria weighted 1 to 3
by importance. Named
criteria weigh 1 each. The options `output`, `criteria` and `entities` override the question,
for clients that already have them as data.

```bash
curl -N -X POST localhost:8080/v1/runs \
  -H 'content-type: application/json' \
  -d '{"method": "ledger", "input": {"question": "Rank the open-weight coding models that fit one 24 GB GPU on coding benchmark score, VRAM at 4-bit and licence"}}'
```

The run then assesses each option on each criterion from the claims closest to that pair, keeping
only claims that name the option. Each cell with no such claim becomes a question of its own,
searched for like the others with a budget of 8 more searches, and assessed again when a search adds
claims. A ranking ranks the options with a finding on each criterion twice, the second time in
reverse order, since models favour what they read first. Options with no finding tie for last place
on that criterion. The overall order is the weighted mean of those places. An option the run chooses
must appear in its first search's results.

The report opens with a table of findings, each cell cited. `matrix` holds the same rows: per option
its overall `rank`, and per criterion its `finding`, `claims` (claim ids, as in `claims`) and
`rank`. A table has no ranks. The dashboard shows a ranking as places coloured from best to worst
in each column, with the finding and its sources on hover.

## Write an article

```bash
curl -N -X POST localhost:8080/v1/runs \
  -H 'content-type: application/json' \
  -d '{"method": "storm", "input": {"question": "The Antikythera mechanism"}}'
```

The response is `text/event-stream`:

```
event: run
data: {"run_id": "0f3c9a1b7e42"}

event: progress
data: {"stage": "research", "detail": "completed conversation turn 3", "at": 1793475466.3, "done": 3, "total": 12, "seq": 7}

event: done
data: {"article": "...", "outline": "...", "citations": [...], "duration_seconds": 412}
```

Stages are `research`, `outline`, `article` and `polish`. A run takes minutes to tens of minutes,
so use a client that streams, and set a generous timeout.

`at` is the Unix time of the event. `done` and `total` count progress through the stage when it has
a countable unit, and are null otherwise: sections in `article`, conversation turns in `research`.
The research `total` is an upper bound, since a conversation can end early. `seq` numbers the run's
events from 0, and a model call's `event` names the event it followed.

Every streaming response opens with a `run` frame. Use its `run_id` to follow the run at
`/v1/runs/{id}` if the client stops reading, and to attribute cost afterwards. A client that does
not know the event name ignores it.

Options: `search_top_k`, `max_conv_turn`, `max_perspective`, `do_polish_article`, `model_fast`,
`model_strong`. Each falls back to the configured default.

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

`GET /` serves the runs page. New and Runs in its header lead to its views, and each view has its
own address, so a reload or a shared link keeps it.

- New (`#/`) starts a run. Pick the method, enter the question or topic, and press Enter or the
  arrow. For research, name the options and criteria in the question itself, as in "rank the Pixel
  9a and the Fairphone 5 on price and updates"; the brief reads them from there. To fix the shape of
  the answer, pick report, ranking or table from the `+` menu in the input box. It shows as a badge
  there, and removing the badge leaves the shape to the question. The page asks the clarifying
  questions first; answer what you can, or skip.

  Beside the input, the plan lists the stages with the budget the settings give each, the most the
  run can search, and how long finished runs of the method took. A change made there is kept as in
  the settings. Below it, the method's recent runs are listed. The page opens on the method last
  picked, and `/` from any page comes back to it with the input focused.
- Runs (`#/runs`) lists every run kept, with its state, elapsed time, tokens and cost.
- A run (`#/runs/<id>`) shows the runs list beside it, and has a tab per view, each with its own
  address, such as `#/runs/<id>/calls`.
  - Result is shown once the run has one, and the run opens on it. Each `[n]` is a badge naming
    the source's site; hovering or focusing one shows its title, URL and quote.
  - Overview shows the stages on a timeline with each stage's duration, including the planned
    stages not reached yet, and the event log. An event that led to model calls links to them.
  - Usage shows elapsed time, totals for tokens in and out, reasoning tokens, cost, searches and
    embedding tokens, then the usage split by stage, by role and by model, with the time spent in
    calls, output tokens per second and the mean time per call.
  - Calls lists every model call with a clock on any still waiting. Opening one shows what it sent
    and what it answered, when `KVASIR_KEEP_CALL_CONTENT` kept it.
  - Searches lists every query sent to SearXNG, with the event it followed, how many results came
    back and why it failed if it did. Opening one lists the results, when
    `KVASIR_KEEP_CALL_CONTENT` kept them.
  - Graph is shown for a run of the `graph` method. It lays out the topic, the first questions
    hanging from it, each follow-up hanging from the question whose evidence raised it, and, once
    the run is done, the claims each question rests on, with the links between claims that
    corroborate or contradict each other. Questions are coloured by status. Drag a node to move
    it, scroll or drag the background to zoom and pan, and select a node to fade all but its
    neighbours and show its queries, or a claim's quote and source. It follows the run live.

Figures update live while the run is in progress. The theme selector in the header offers
`system`, `light`, `dark`, `paper`, `riso`, `amber`, `moss` and `navy`, and the choice is kept in
the browser. The page loads nothing from another origin, since the image is read-only and offline.
Its source is in [`web/`](web/); see [Work on the runs page](#work-on-the-runs-page).

The same data is available as JSON.

| Request | Effect |
| --- | --- |
| `GET /v1/runs` | Every run kept, newest first. `has_result` says whether its detail carries a result. |
| `GET /v1/runs/{id}` | One run, with per-stage timings, usage, its last 2000 model calls, its last 1000 searches, its last 1000 events, the questions a `graph` run has asked so far, and its result. |
| `GET /v1/runs/{id}/calls/{n}` | What call `n` sent and answered: `messages`, `response` and `reasoning`. 404 unless `KVASIR_KEEP_CALL_CONTENT` kept it. |
| `GET /v1/runs/{id}/searches/{n}` | What search `n` returned: `results`, each with `title`, `url` and `snippet`, before the run drops what it already read. 404 unless `KVASIR_KEEP_CALL_CONTENT` kept it. |
| `GET /v1/runs/{id}/events` | Follow a run live. Streams. Opens and closes with a `snapshot`, and sends a `status` with the last 50 calls and the last 50 searches after each `progress` and every 2 seconds in between. |
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
them. A usage row's `seconds` sums the time its successful calls took, from request to answer:
nothing is streamed, so it includes the time the model spent reading the prompt.

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
and ends. What calls sent and answered, and what searches returned, is kept only with
`KVASIR_KEEP_CALL_CONTENT=true`, in the same store as the run and for as long as it. Their timings,
token counts, queries and result counts are kept either way.
Memory is the default because a client such as Open WebUI keeps its own record of every
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
| `KVASIR_MODEL_CHECKER` | unset | Research: a grounding checker such as MiniCheck, which answers Yes or No. Unset leaves every check to the fast model. |
| `KVASIR_DATA_DIR` | `/data` | Writable directory for sessions, and for the run database. |
| `KVASIR_SESSION_TTL_HOURS` | `168` | Session expiry, applied at startup. |
| `KVASIR_RUN_STORE` | `memory` | `memory`, or `sqlite` to keep runs across restarts in `$KVASIR_DATA_DIR/kvasir.db`. |
| `KVASIR_MAX_RUNS` | `100` | Runs kept, most recent first, in either store. |
| `KVASIR_RUN_TTL_DAYS` | `30` | Age past which a stored run is deleted. `sqlite` only. |
| `KVASIR_KEEP_CALL_CONTENT` | `false` | Keep each model call's messages and answer, and each search's results, with its run, to read on the runs page. `true` or `false`. |
| `KVASIR_MAX_CONCURRENT_RUNS` | `1` | Concurrent runs. Beyond this, requests get 429. |
| `KVASIR_MAX_THREADS` | `10` | Width of each thread pool, and the cap on outbound search and page requests. |
| `KVASIR_SEARCH_TOP_K` | `3` | Search results per query. |
| `KVASIR_MAX_CONV_TURN` | `3` | Questions per perspective. |
| `KVASIR_MAX_PERSPECTIVE` | `3` | Perspectives to research. |
| `KVASIR_RESEARCH_MAX_SEARCHES` | `16` | Research: searches in a run, before the 8 for empty cells. |
| `KVASIR_RESEARCH_RESULTS_PER_QUERY` | `3` | Research: pages read per search. |
| `KVASIR_RESEARCH_MAX_ATTEMPTS` | `4` | Research: queries tried on one question before it is given up. |
| `KVASIR_RESEARCH_FOLLOW_UPS` | `true` | Research: whether an answered question can add harder ones. `true` or `false`. |
| `KVASIR_RESEARCH_MAX_DEPTH` | `2` | Research: levels of follow-ups below the first questions. |
| `KVASIR_RESEARCH_MAX_ENTITIES` | `8` | Research: options a ranking or table compares, when the run chooses them. |
| `KVASIR_RESEARCH_MAX_CRITERIA` | `5` | Research: criteria, when the run chooses them. |
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
`uvicorn kvasir.api.app:app` to get that: the module configures logging first and tells uvicorn to
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

One Pipe function lives under [`owui/`](owui/), exposing research, STORM and Co-STORM
as three models. Research asks its clarifying questions as a first reply. It writes a run's progress into the message as it happens, turns sources into citations,
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

### Work on the runs page

The page is a Svelte app in `web/`, built with Vite into `src/kvasir/static/`, which is not
committed. It needs Node 26 and pnpm.

```bash
cd web
pnpm install
pnpm dev:mock   # the page on localhost:5173, its API answered from web/fixtures/
pnpm dev        # the same, with /v1 proxied to a kvasir on localhost:8080
pnpm build      # into src/kvasir/static/, where the service serves it
```

`pnpm dev:mock` needs no gateway, search engine or run. Each file in `web/fixtures/` is a run named
after it, and `#/runs/<name>/result` opens its result directly: `#/runs/ranking/result` shows the
ranking table. Two more runs are made from `ranking.json`: `running`, frozen partway, and `live`,
replayed at 20 times its real pace and started again once it ends. Set `MOCK_SPEED` to change the
pace. `graph-live` replays `graph.json` the same way, its questions appearing as the run goes. A
run started from the page replays a fixture the same way: `wal.json` for research, and
`article.json` for an article.

`<name>.calls.json` holds what a fixture's calls sent and answered, keyed by call id, and
`<name>.searches.json` what its searches returned, keyed by search id. Only `wal` has the first and
only `graph` the second, both recorded with `KVASIR_KEEP_CALL_CONTENT=true`.

A fixture is a run's detail as `GET /v1/runs/{id}` returns it. To add one from a run kept in the
SQLite history:

```bash
curl -s localhost:8080/v1/runs/<run id> > web/fixtures/<name>.json
```

To include its calls' content, with the run kept by a service started with
`KVASIR_KEEP_CALL_CONTENT=true`:

```bash
run=<run id>
for n in $(jq -r '.calls[] | select(.has_content) | .id' web/fixtures/<name>.json); do
  curl -s localhost:8080/v1/runs/$run/calls/$n | jq -c --arg n "$n" '{($n): .}'
done | jq -s -c add > web/fixtures/<name>.calls.json
```

Its searches' results are fetched the same way:

```bash
for n in $(jq -r '.searches[] | select(.has_content) | .id' web/fixtures/<name>.json); do
  curl -s localhost:8080/v1/runs/$run/searches/$n | jq -c --arg n "$n" '{($n): .}'
done | jq -s -c add > web/fixtures/<name>.searches.json
```

The TypeScript types in `web/src/lib/schema.d.ts` are generated from the service's OpenAPI schema.
Run `pnpm types` after changing a schema in `src/kvasir/schemas/`; CI fails when they differ.

```bash
pnpm check   # svelte-check
pnpm test    # Vitest: the markdown renderer
pnpm e2e     # Playwright, against the mock and against the built page served by the service
```

`pnpm e2e` needs Chromium once: `pnpm exec playwright install chromium`.

### Evaluate research

[`eval/questions.toml`](eval/questions.toml) holds 20 questions: 10 reports, 6 rankings and 4
tables. Each lists the output type its wording asks for and a few checks a reader of the question
would need answered. To evaluate, start the service, then run:

```bash
uv run python -m kvasir.research.evaluate data/eval/baseline
```

It researches each question through the service at `http://localhost:8080` (`--url` to change),
without clarifications, and saves each result as `<id>.json`. `--method` picks the method, `ledger`
by default, and `--options` takes its options as a JSON object, such as `'{"max_searches": 8}'`.
Save each method or set of options to its own directory to compare them. Scoring reads `report`,
`claims` and `citations`, so `ledger` and `graph` results score and `storm` results do not. A saved result is not researched
again, so an interrupted evaluation resumes; `--only <id>...` limits it to some questions. A run
takes minutes, so the full set takes hours on a local model.

Every saved result is then scored, with the strong model as judge:

- **Output.** Whether the brief chose the expected output type.
- **Checks.** A yes or no for each of the question's checks, against the report.
- **Citations.** Each cited sentence of the final report, against the quotes of the claims read
  from the sources it cites. Verification already removed what it judged unsupported, so this
  measures what slipped past it.

Scores, with every failed check and unsupported sentence and the judge's reason, go to
`scores.json` beside the results. A summary table is printed. The judge samples at temperature
1.0 like every role, so a score can move by a check or two between two scorings of the same
result.

## Licence

Dual MIT and Apache-2.0, at your option. See [LICENSE-MIT](LICENSE-MIT) and
[LICENSE-APACHE](LICENSE-APACHE).

STORM and Co-STORM are the work of [Stanford OVAL](https://github.com/stanford-oval/storm). Their
`knowledge_storm` package is vendored and modified under `src/kvasir/storm`, which stays MIT only,
as upstream is. See [`src/kvasir/storm/NOTICE`](src/kvasir/storm/NOTICE) for the fork point and
[`docs/fork-notes.md`](docs/fork-notes.md) for what was changed.
