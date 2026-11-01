# Research methods: plan

Status: written 2026-11-01. Step 1 of the [build order](#build-order) is built; the rest is planned.

kvasir has one route per engine: `/v1/research` for the question ledger, `/v1/article` for STORM.
Each has its own request model, run kind, stage list, role table, runs-page entry and Open WebUI
model. This plan replaces them with one run route that takes a method name, so methods can be
added, chosen and compared without touching the service around them. It then adds a second
research method, `graph`, which explores what the research learns recursively rather than from a
flat list of questions.

How the service works today is in [`architecture.md`](architecture.md), and the research pipeline
in [`research.md`](research.md). This page is the plan and the reasoning behind it.

## Decisions taken

| Decision | Reason |
| --- | --- |
| One route, `POST /v1/runs`, with a `method` field. `/v1/research` and `/v1/article` are removed, not kept as aliases. | Two ways to start the same run is two contracts to keep. The release is a breaking change. |
| Co-STORM stays out of the method registry, on its `/v1/session` routes. | It is a session spanning many requests, not one run, and it is not worth bending the run contract around. |
| `deep/` is renamed `research/`. | It holds what every research method shares, not one deep pipeline. |
| Modules hold one responsibility, around 300 lines at most. | `deep/pipeline.py` is 1031 lines with the run built from closures, so no second method can reuse its steps. |
| The vendored `storm/` is not reorganised. | Its changes from upstream are tracked in [`fork-notes.md`](fork-notes.md); moving files would hide them. |

## Design rules, and the evidence behind them

Confidence follows [`deep-research-plan.md`](deep-research-plan.md): "high" is a primary source
read in full, "medium" a primary source read through a summary or abstract, "low" a secondary
source or a fragment.

| Rule | Evidence | Confidence |
| --- | --- | --- |
| Methods differ in orchestration only. Search, fetch, intake, verification and rendering are shared. | Head-to-head comparisons are fair only when the retriever is fixed, which is how [BrowseComp-Plus](https://github.com/texttron/BrowseComp-Plus) isolates the agent from the search. No surveyed framework shares a claim store across its modes. | medium |
| A method's options are typed per method, and unknown keys are refused. | STORM's typed runner arguments against GPT Researcher's flat keyword arguments, where every mode's options share one namespace. [STORM](https://github.com/stanford-oval/storm), [GPT Researcher](https://docs.gptr.dev/docs/gpt-researcher/gptr/pip-package) | medium |
| Whether a branch is worth exploring, and when it is spent, is decided in code from the graph, not by asking the model. | No published method has a validated stop rule for a branch that works with a small model; the rules found are depth or budget caps, or a model's judgement. The research loop already keeps this rule ([`research.md`](research.md#what-it-is-built-for)). | low: an absence of evidence |
| Each exploration step reads only its own path through the graph, not the whole run. | Context rot, as in the existing plan. Atom of Thoughts contracts answered sub-questions into their parent to keep each step small. [Atom of Thoughts](https://arxiv.org/abs/2502.12018) | low for Atom of Thoughts: not read |
| Writing draws evidence per section from a store, through an outline that points into it. | WebWeaver reports state-of-the-art results on DeepResearch Bench with a citation-grounded outline over a memory bank. [WebWeaver](https://arxiv.org/abs/2509.13312) | medium: abstract only, no figures read |
| A claim matched against its quote is attributed, not verified true. Truth needs independent sources agreeing. | Attribution checkers (MiniCheck, AlignScore, NLI models) check support by a given text only. Corroboration needs a second source. [LLM-AggreFact](https://llm-aggrefact.github.io/) | high for the distinction |
| A small grounding checker may replace the model for claim-against-quote checks. | On LLM-AggreFact, MiniCheck-Flan-T5-L (0.8B) averages 75.0 and Bespoke-MiniCheck-7B 77.4, against 75.6 for Qwen2.5-72B and 71.8 for QwQ-32B prompted. [LLM-AggreFact](https://llm-aggrefact.github.io/), [Bespoke-MiniCheck-7B](https://huggingface.co/bespokelabs/Bespoke-MiniCheck-7B) | medium: one leaderboard snapshot, to check on kvasir's own claims |

## Layout

```text
src/kvasir/
  api/
    app.py            lifespan, static files, health and readiness
    streaming.py      registering a run, claiming a slot, the SSE response
    runs.py           POST /v1/runs, GET /v1/runs and its sub-routes
    methods.py        GET /v1/methods
    sessions.py       the Co-STORM routes
    tasks.py          /v1/tasks and /v1/clarify
  schemas/
    requests.py       RunRequest, the input, clarifications
    results.py        the result every method returns, claims, citations
    runs.py           run summaries, details and usage as served
  runs/
    registry.py       Run, RunRegistry
    usage.py          usage by model, role and stage
    store.py          the SQLite history
    progress.py       ProgressStream and the stage names
  research/           shared by every research method
    calls.py          calling a model with a signature
    search.py         searching, skipping URLs already read
    fetch.py          downloading a page or a PDF as text, splitting it into pieces
    read.py           fetching the hits, filtering pieces, intake
    text.py           matching quotes and names word for word
    claims.py         Claim, lookups by meaning, claims as a prompt lists them
    verify.py         checking cited sentences
    write.py          one section from its closest claims
    render.py         markdown and numbered sources
    clarify.py        the questions asked before a run
    signatures.py     signatures more than one method uses
    evaluate.py       scoring runs on eval/questions.toml
  methods/
    __init__.py       METHODS, the registry
    base.py           the Method protocol and the options base
    ledger/           today's research pipeline
      method.py       the run, orchestration only
      brief.py        choosing what to compare
      questions.py    Question, adding to the ledger, the ledger as notes and results
      loop.py         the question loop, picking and judging a question
      compare.py      assessing, filling and ranking the matrix
      repair.py       searching again for removed sentences
      signatures.py
    graph/
      method.py
      graph.py        nodes, edges, merging claims
      frontier.py     worth score, picking, when a branch is spent
      explore.py      the recursion
      signatures.py
    storm/
      method.py       wraps the STORM runner
  storm/              vendored, unchanged
```

A signature lives next to its only user. One used by several methods moves to
`research/signatures.py`.

## The method contract

```python
class Method(Protocol):
    name: str
    Options: type[BaseModel]  # extra="forbid"
    roles: dict[str, tuple[Tier, int]]  # replaces the tables in runners.py

    def stages(self, options: BaseModel) -> list[str]: ...
    def run(
        self, settings: Settings, input: Input, options: BaseModel, stream: ProgressStream
    ) -> RunResult: ...
```

`POST /v1/runs` takes `{method, input: {question, clarifications}, options}`, with `options`
validated by the method's `Options`. The response is today's SSE stream, unchanged.

`GET /v1/methods` returns, per method, its options' JSON Schema with the configured defaults
filled in, its stages, and whether it asks clarifying questions. It replaces `GET /v1/defaults`.
The runs page and the Open WebUI pipe build their method lists from it, which removes the copies of
pipeline constants in `web/src/lib/methods.ts`.

Every result carries `report`, `citations`, `claims` and `duration_seconds`; a method adds its own
fields. `python -m kvasir.research.evaluate` gains `--method` and `--options`, so methods are
scored on the same questions.

## The graph method

The ledger already holds a tree, since a follow-up keeps its parent and depth, but it searches it
as a flat list, adds a fixed two follow-ups to any answered question, and stops only on the whole
run's budget or stall. The graph method makes the tree the thing the run reasons over.

| Part | Holds |
| --- | --- |
| Claim node | Its text, its sources (each a URL and a quote), and its verification state. |
| Question node | Something still to find out: the frontier. |
| Edges | A question is answered by claims; a claim raises questions; claims corroborate or contradict each other. |

- **Merging.** A new claim close in meaning to an existing one, and supported by the same
  checker, adds a source to it instead of becoming a node. The number of independent domains per
  claim falls out of this.
- **Recursion.** Exploring a question searches for it, takes in the claims, and asks the child
  questions those claims raise. Each call reads the question's path from the root and its new
  claims, not the whole graph.
- **Worth.** A score computed in code orders the frontier, best first: closeness to the root
  question, distance from what the graph already holds, gaps (a claim with one source, or a
  contradiction edge), and a penalty per level of depth. A branch's share of the search budget
  follows its score.
- **Spent.** A branch closes when a set number of searches on it in a row add no node and no
  source, or when it reaches its depth or budget cap.
- **Writing.** Sections come from clusters of the graph, through the shared `write` and `verify`.

This design is kvasir's own. No source was found applying a graph stop rule like this one, so the
method is an experiment, kept or dropped on evaluation against `ledger`.

A smaller experiment comes first, if the evaluation is cheap enough to run twice: the worth score,
merging and the per-branch stop added to `ledger`. If that closes most of the gap, the graph
method is not worth its code.

## Verification, in tiers

Shared in `research/`, used by every method. Each tier runs only on what the cheaper one before it
leaves undecided.

| Tier | Checks | Cost |
| --- | --- | --- |
| 1. Quote check | The quote is on the page, word for word. Exists today. | None |
| 2. Grounding checker | The quote supports the claim. | One small-model call |
| 3. Model judge | Sentences combining several claims, and checker scores near the threshold. Exists today for sentences. | One `verify` call |
| 4. Corroboration | At least two distinct domains support the claim. | Checker calls between merged sources |
| 5. Contradiction | Claims about the same thing from different sources disagree. | Checker calls between pairs |

Tier 2 is optional, configured as its own model. Without it, tier 3 does its work.

## Build order

Each step keeps the tests passing, and steps 1 and 2 change no behaviour.

1. Rename `deep/` to `research/`, and split `pipeline.py` into `research/` and `methods/ledger/`.
2. Split `main.py`, `models.py` and `runs.py` into `api/`, `schemas/` and `runs/`.
3. Add `methods/base.py`, the registry, `POST /v1/runs` and `GET /v1/methods`; port `ledger`
   and `storm`; remove `/v1/research`, `/v1/article` and `/v1/defaults`; move the runs page, the
   Open WebUI pipe and the evaluation to the new routes.
4. Run the evaluation on `ledger` and keep the scores as the baseline.
5. Add the verification tiers, behind settings, and evaluate each.
6. Add the worth score, merging and per-branch stop to `ledger`, and evaluate.
7. Build `graph`, and evaluate it against both.

## Open decisions

- **The grounding checker's model.** Bespoke-MiniCheck-7B is licensed CC BY-NC 4.0; an earlier
  revision said Apache-2.0. MiniCheck-Flan-T5-L is smaller but is not a chat model, so the gateway
  may not serve it. Whether a second model can stay loaded next to the main one, or whether Ollama
  swaps them, is to be measured on the target machine. If it swaps, the checks of a run should be
  batched into one stage so it swaps once.
- **A benchmark beyond `eval/questions.toml`.** DeepResearch Bench scores reports and citations,
  and its judge can point at a local model, which makes its scores comparable only between kvasir's
  own methods. BrowseComp-Plus needs its fixed corpus, which means a retriever beside SearXNG.
