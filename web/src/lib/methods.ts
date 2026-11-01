// The ways to run, and what each needs to be shown before it starts. A new method is an entry here:
// the new run page draws its field, its options, its stages and their budgets from it.
import type { Defaults } from "./api";
import type { IconName } from "../components/Icon.svelte";
import { value, type Kind } from "./settings.svelte";

type Values = Partial<Defaults>;
// The options picked from the "+" menu, by request field. One left out is the run's to decide.
export type Picked = Record<string, string>;

// Something the request can ask for beyond the question. The question itself carries the rest:
// the brief takes the options and criteria it names.
export interface Option {
  // The request field it fills.
  name: string;
  label: string;
  choices: { value: string; label: string; icon: IconName; hint: string }[];
}

export interface Stage {
  name: string;
  // What the stage does, with the budget the current settings give it.
  does: (v: Values, picked: Picked) => string;
  settings: (keyof Defaults)[];
  // When the run decides whether the stage happens, or why it will not.
  note?: (v: Values, picked: Picked) => string | null;
  skipped?: (v: Values, picked: Picked) => boolean;
}

export interface Method {
  kind: Kind;
  // The `kind` its runs are listed under.
  run: string;
  label: string;
  blurb: string;
  endpoint: string;
  field: "question" | "topic";
  placeholder: string;
  // Whether `/v1/research/clarify` is asked first.
  clarify: boolean;
  options: Option[];
  stages: Stage[];
  // The most the run can spend, in the units that bound it.
  size: (v: Values, picked: Picked) => string;
}

// Mirrors `FILL_SEARCHES` in `kvasir/methods/ledger/compare.py`: searches for the cells of a table
// still empty after the research, on top of its budget.
const FILL_SEARCHES = 8;

// The output asked for, or null when the brief is to pick it.
const output = (picked: Picked) => picked.output ?? null;
const compares = (picked: Picked) => output(picked) !== "report";

export const METHODS: Method[] = [
  {
    kind: "research",
    run: "research",
    label: "Deep research",
    blurb: "Answers a question from searched sources, as a cited report, a ranking or a table.",
    endpoint: "/v1/research",
    field: "question",
    placeholder: "Rank the best phones of 2026 on price, performance and privacy",
    clarify: true,
    options: [
      {
        name: "output",
        label: "Answer as",
        choices: [
          { value: "report", label: "Report", icon: "page", hint: "cited prose" },
          { value: "ranking", label: "Ranking", icon: "ranking", hint: "options ordered on criteria" },
          { value: "table", label: "Table", icon: "table", hint: "options compared on criteria" },
        ],
      },
    ],
    stages: [
      { name: "brief", does: () => "Pins down the question, its assumptions, and the shape of the answer.", settings: [] },
      {
        name: "background",
        does: (v) => `Finds up to ${v.max_entities} options and ${v.max_criteria} criteria to compare, past those the question names.`,
        settings: ["max_entities", "max_criteria"],
        note: (_, i) =>
          !compares(i) ? "skipped for a report" : output(i) ? "unless the question names both" : "only for a ranking or a table",
        skipped: (_, i) => !compares(i),
      },
      {
        name: "research",
        does: (v) =>
          `Up to ${v.max_searches} searches, reading ${v.results_per_query} pages each, ${v.max_attempts} attempts per question` +
          (v.follow_ups ? `, follow-ups ${v.max_depth} level${v.max_depth === 1 ? "" : "s"} deep.` : ", no follow-ups."),
        settings: ["max_searches", "results_per_query", "max_attempts", "follow_ups", "max_depth"],
      },
      {
        name: "assess",
        does: () => `Fills each cell of the table from the evidence, with up to ${FILL_SEARCHES} more searches for empty ones.`,
        settings: [],
        note: (_, i) => (!compares(i) ? "skipped for a report" : output(i) ? null : "only for a ranking or a table"),
        skipped: (_, i) => !compares(i),
      },
      { name: "write", does: () => "Writes the answer, each claim citing its source.", settings: [] },
      { name: "verify", does: () => "Checks each cited sentence against its quote, and removes what is unsupported.", settings: [] },
    ],
    size: (v, i) => {
      const searches = v.max_searches ?? 0;
      const fill = !compares(i) ? "" : output(i) ? ` and ${FILL_SEARCHES} for empty cells` : `, ${FILL_SEARCHES} more for a table`;
      return `At most ${searches} searches${fill}, reading ${searches * (v.results_per_query ?? 0)} pages.`;
    },
  },
  {
    kind: "article",
    run: "storm",
    label: "Article",
    blurb: "Writes a Wikipedia-style article on a topic, from simulated expert conversations (STORM).",
    endpoint: "/v1/article",
    field: "topic",
    placeholder: "The history of the Kvasir stone",
    clarify: false,
    options: [],
    stages: [
      {
        name: "research",
        does: (v) =>
          `${v.max_perspective} perspectives ask ${v.max_conv_turn} questions each, reading ${v.search_top_k} results per search.`,
        settings: ["max_perspective", "max_conv_turn", "search_top_k"],
      },
      { name: "outline", does: () => "Drafts an outline from the conversations.", settings: [] },
      { name: "article", does: () => "Writes each section, citing its sources.", settings: [] },
      {
        name: "polish",
        does: () => "Adds a lead section and removes repetition.",
        settings: ["do_polish_article"],
        note: (v) => (v.do_polish_article ? null : "skipped"),
        skipped: (v) => !v.do_polish_article,
      },
    ],
    size: (v) => `${(v.max_perspective ?? 0) * (v.max_conv_turn ?? 0)} conversation turns, each with its own searches.`,
  },
];

export const MODELS: (keyof Defaults)[] = ["model_fast", "model_strong"];

// Every value the stages read, defaults included, so a plan reads as the run would go.
export const values = (method: Method, defaults: Values): Values =>
  Object.fromEntries(method.stages.flatMap((stage) => stage.settings).map((name) => [name, value(name, defaults)]));
