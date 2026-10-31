# Deep research pipeline: plan

Status: phases 0 to 2 built (report output, clarify, dashboard). Written 2026-10-31.

STORM writes an encyclopedic article about a topic. It takes no instructions, does not compare or
rank, and its prompts ask for a Wikipedia page. This plan adds a second pipeline, `deep`, for
questions such as "rank the best companies in sector X on these criteria", built for a 27B-class
model served locally with a 16k context. It takes over `/v1/research`; STORM moves to
`/v1/article`, named for what it produces, as `/v1/session` is for Co-STORM.

## Design rules, and the evidence behind them

Each rule names the finding it rests on. Confidence is how directly the source was read: "high" is
a primary source read in full, "medium" a primary source read through a summary, "low" a secondary
source or a fragment.

| Rule | Evidence | Confidence |
| --- | --- | --- |
| Code decides how much to search; the model only decides what to search for. | Prompted Qwen3-32B made fewer than one search call per question on BrowseComp-Plus despite being told to search, and scored 3.5 to 10%. Given the right documents it scored 83%. [BrowseComp-Plus](https://arxiv.org/html/2508.06600v1) | medium |
| Prompting is enough; no training. | On the same QwQ-32B base, prompted Search-o1 scored 48.1 on GAIA, trained WebDancer 47.4, RL-trained ASearcher 58.7. [ASearcher](https://arxiv.org/html/2508.07976v4) | medium |
| No call sees more than one page piece, the notes, and the question. | Context rot: Qwen3-8B degrades around 5,000 words, and one distractor already hurts. [Chroma](https://www.trychroma.com/research/context-rot) | high |
| Rebuild the working notes each round; never carry the history. | IterResearch keeps the question, an evolving report and the current item, and drops dead ends: 49.1 against 36.5 for accumulated context, with training confounded. [IterResearch](https://arxiv.org/html/2511.07327v2) | high for the mechanism |
| Page intake returns structured claims with a confidence, or nothing. | Removing structured output and confidence from LLM×MapReduce dropped its score from 41.2 to 25.9. [LLM×MapReduce](https://arxiv.org/html/2410.09342v1) | medium |
| Pre-filter page pieces by embedding before any model call. | GPT Researcher filters 1,000-character chunks by embedding similarity before writing. [compression.py](https://github.com/assafelovic/gpt-researcher/blob/master/gpt_researcher/context/compression.py) | medium |
| Every cited sentence is checked against its quote. | Untrained Llama-3.1-8B scores 19.7 citation F1 on LongBench-Cite, against 65.6 for GPT-4o. [LongCite](https://arxiv.org/html/2409.02897v3) | high |
| Write sections in order, sharing the outline, not in parallel. | LangChain dropped parallel section writers because reports came out disjoint. [open_deep_research](https://www.langchain.com/blog/open-deep-research) | medium |
| Rank relative to each other; never trust a small model's absolute score. | Pairwise ranking beats pointwise by over 10 points NDCG@10 ([PRP](https://arxiv.org/html/2306.17563v2)); small judges give poor absolute scores but a usable ordering ([Thakur et al.](https://arxiv.org/html/2406.12624)); 7B listwise rerankers are close to GPT-4 ([FIRST](https://arxiv.org/html/2406.15657)). | medium |
| Run each ranking twice with the input order reversed. | LLM position bias is systematic. [Shi et al.](https://arxiv.org/html/2406.07791v4) | medium |
| Criteria come from one checklist call, editable by the user. No expert persona. | Checklists before judging: 46.4 to 52.2% agreement with humans ([TICK](https://arxiv.org/html/2410.03608v1)). Persona prompts do not raise accuracy and their effect is "largely random" ([Zheng et al.](https://arxiv.org/abs/2311.10054)). | medium |
| One process, no parallel agents. | Ollama answers one request at a time, so parallel agents buy no speed. Cognition reports conflicting decisions between parallel agents. [Cognition](https://cognition.com/blog/dont-build-multi-agents) | medium |

Deferred until there is an evaluation to prove them: thinking mode per step, `dspy.RLM` over the
evidence store, a dedicated fact-checking model such as MiniCheck, and pairwise against listwise
ranking.

## Pipeline

```
clarify      [separate call, optional] question -> up to 3 questions for the user, or none
brief        question + clarifications -> output type, scope, entities and criteria if the question names them
background   2-3 searches, snippets only -> domain context (a short paragraph)
criteria     [ranking, table] one checklist call over the background, unless the user gave them
entities     [ranking, table] candidates from background snippets, capped, unless the user gave them
rounds       repeat until a budget is spent or a round adds too few claims:
  plan         model: queries_per_round queries from question + notes + gaps
  search       SearXNG, results_per_query results each, skipping URLs already read
  fetch        page text, split into pieces
  filter       keep the pieces closest to the question by embedding
  intake       per piece: claims with a verbatim quote, entities, confidence; or nothing relevant
  notes        rebuild the notes from the store: what is known, what is missing, dead ends
fill         [ranking, table] targeted searches for each entity x criterion cell with no claim
judge        [ranking] per criterion, rank entities from their claims, twice, input order reversed
outline      from notes and claims, shaped by the output type
write        one section at a time: its claims, the outline, a summary of earlier sections
verify       each cited sentence against its claim's quote; unsupported sentences are removed
render       markdown; ranking and table outputs add a matrix built in code, not by the model
```

Output types share everything up to `outline`. Only the outline prompt, the section prompt and
`render` differ.

| Output | Outline | Extra |
| --- | --- | --- |
| `report` | findings first, then sections | none |
| `article` | encyclopedic sections | none |
| `ranking` | method, ranking, one section per entity | criteria, entities, fill, judge; ranked table |
| `table` | short summary | criteria, entities, fill; matrix of findings, no ranking |

## Where it lives

New code goes in `src/kvasir/deep/`, outside the vendored fork, so it is typed, linted and checked
by `mypy` like the rest of the service.

| File | Holds |
| --- | --- |
| `deep/store.py` | `Source`, `Claim` (id, text, quote, url, entities, confidence, round), and lookups by entity and by embedding |
| `deep/fetch.py` | page download with `httpx`, inside `runtime.fetch_slot()`; text extraction with `beautifulsoup4`; splitting into pieces |
| `deep/signatures.py` | one `dspy.Signature` per model step: `Brief`, `PlanQueries`, `Intake`, `RebuildNotes`, `Criteria`, `Entities`, `RankCriterion`, `Outline`, `WriteSection`, `Verify` |
| `deep/pipeline.py` | `run_deep_research(settings, request, stream) -> DeepResearchResult`, the stage order and the budgets |
| `deep/render.py` | markdown, the ranking table, and the citation list |

Reused unchanged:

| Piece | From |
| --- | --- |
| Streaming, run slots, the run registry | `main._streamed`, `runs.RunRegistry` |
| Progress events and stage timings | `progress.ProgressStream.publish(stage, detail, done, total)` |
| Models and usage accounting | `runners._language_model`, `storm.lm.GatewayModel` |
| Embeddings | `storm.encoder.Encoder`, `cosine_similarity` |
| Search | `storm.rm.SearXNG` |
| Fetch concurrency cap | `storm.runtime.fetch_slot` |
| Thread pools that keep the run's context | `storm.runtime.ContextThreadPoolExecutor` |

## API

| Route | Before | After |
| --- | --- | --- |
| `POST /v1/article` | did not exist | STORM, unchanged apart from the path |
| `POST /v1/research` | STORM | the deep research pipeline |
| `POST /v1/research/clarify` | did not exist | questions to ask before a research run |

Moving STORM breaks every client of `/v1/research`. The Open WebUI pipe, the tests and the README
change in the same commit, which is marked `feat!` so release-please bumps the minor version, as it
does for breaking changes before 1.0. The run kind stays `storm`.

### Clarifying before a run

OpenAI's Deep Research asks before it starts. The API stays stateless: clarifying is its own call,
and its answers travel with the research request.

```
POST /v1/research/clarify  {"question": "..."}
  -> {"questions": ["Which region?", "Listed companies only?"]}   empty when the question is clear
POST /v1/research          {"question": "...", "clarifications": [{"question": "...", "answer": "..."}]}
```

`clarify` is one fast-model call, like `/v1/tasks`: no run is registered and no slot is taken. A
client may skip it. `brief` reads the clarifications, and the result states what it still had to
assume.

### Research

`POST /v1/research`, streamed like every run: a `run` frame, `progress` frames, then `done` or
`error`. Stages: `brief`, `background`, `research`, `assess`, `write`, `verify`.

```json
{
  "question": "Which European battery makers are best placed for 2030?",
  "clarifications": [],
  "output": "ranking",
  "criteria": ["production capacity", "cell chemistry", "funding"],
  "entities": null,
  "max_rounds": 4,
  "queries_per_round": 4,
  "results_per_query": 3,
  "model_fast": null,
  "model_strong": null
}
```

Only `question` is required. `output` defaults to what `brief` infers, falling back to `report`.

The `done` frame carries `content` (markdown), `citations` (the existing `Citation` model),
`ranking` (per entity: rank, and per criterion its rank and claim ids) when the output is a
ranking, `claims` (the full evidence store, so a reader can audit any sentence), `removed` (the
sentences `verify` dropped, with the reason), and `duration_seconds`.

Defaults sized for qwen3.8:27b at 16k context:

| Setting | Default | Why |
| --- | --- | --- |
| piece size | about 6,000 characters | about 1,500 tokens, leaving room for notes, instructions and output |
| pieces kept per page | 3 | the embedding filter's cut |
| notes cap | about 1,500 tokens | rebuilt each round, so it never grows |
| `max_rounds` | 4 | |
| new-claim stop | fewer than 5 new claims in a round | saturation |
| entities cap | 8 | ranking cost grows with entities x criteria |

## Dashboard

With no Open WebUI in front of it, the runs page at `/` becomes the way to use kvasir, so it gains
a launch panel. It stays one self-contained file with no external asset.

- **Launch.** A form with a kind (`research` or `article`), the question or topic, and for research
  the output type and optional criteria and entities. Run settings sit behind a disclosure.
- **Clarify.** For research, the form calls `/v1/research/clarify` first and shows any questions
  with an answer field each, plus a "skip" that starts without answering.
- **Follow.** The form posts the run, reads the `run` frame for its id, and follows the run
  through `/v1/runs/{id}/events` like any other run. It keeps reading the posted stream and
  discards it, because the server releases the run slot when that response ends, not when the work
  does.
- **Result.** The run detail renders the result: markdown converted by a small renderer covering
  what `render.py` emits (headings, paragraphs, lists, links, emphasis, tables), citations, and for
  research the removed sentences and the evidence store. Every string is escaped before it becomes
  HTML, since it comes from web pages.

The service has no authentication and binds `0.0.0.0`. A launch button changes nothing about who can
start a run, since the API already allows it, but it makes it easier.

## Build order

Each phase ends with its tests passing (dspy's `DummyLM` for model calls, a fake `httpx` for pages,
as `tests/test_storm_signatures.py` already does) and one live run against the local Ollama and
SearXNG.

0. **Move STORM to `/v1/article`.** Route, pipe, tests, README. Done when the suite passes and the
   pipe's STORM model works against the new path.
1. **Report, end to end.** `clarify`, `store`, `fetch`, the rounds, `outline`, `write`, `render`,
   the route. No verification yet. Done when a `report` run on one question streams to `done` with
   cited sections.
2. **Dashboard.** Launch panel, clarify step, result view. Done when a research and an article run
   can be started, followed and read from the page alone.
3. **Verification.** `Verify`, `removed` in the result and on the page. Done when a run reports how
   many sentences it removed, and a test shows an unsupported sentence is dropped.
4. **Ranking and table.** `Criteria`, `Entities`, `fill`, `RankCriterion`, the reversed second
   pass, weighted aggregation in code, the matrix, and their form fields. Done when a ranking run
   returns a ranked table whose every cell links to claims.
5. **Open WebUI.** A research model in `owui/pipe.py`, with the clarify step as a first reply.
6. **Evaluation.** About 20 questions, a judge rubric, and a citation check, then the deferred
   experiments: thinking per step, `dspy.RLM` (present in the pinned dspy 3.3.0), MiniCheck,
   pairwise against listwise.

## Open decisions

1. **Page text extraction.** `beautifulsoup4` is already a dependency and needs no new code beyond
   dropping scripts, styles and navigation. `trafilatura` extracts main content better but was
   removed from the fork with its dependency tree. Proposed: `beautifulsoup4` first, and switch only
   if intake shows it reading menus and footers.
2. **PDFs.** Many company and sector sources are PDFs. Proposed: skip them in phase 1 and decide
   after seeing how many results are PDFs.
3. **Keeping a run's evidence.** Runs live in memory only. Proposed: return the store in the result
   and persist nothing.
