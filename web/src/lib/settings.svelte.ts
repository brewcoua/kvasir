// Kept in localStorage, and only what the user changed: a field left alone follows the configured
// default, read from /v1/methods and shown as its value.
import type { Defaults } from "./api";

export type Value = number | boolean | string;
export type Kind = "research" | "graph" | "article";

export interface Setting {
  name: keyof Defaults;
  label: string;
  hint: string;
  type: "number" | "checkbox" | "text";
  // The least a number may be. 1 when left out.
  min?: number;
  // The kinds of run it is sent with.
  for: Kind[];
}

export const PANES: { name: string; label: string; title: string; settings: Setting[] }[] = [
  {
    name: "research",
    label: "Research",
    title: "Research",
    settings: [
      { name: "research_minutes", label: "Research minutes", type: "number", for: ["graph"], hint: "Graph only: minutes spent searching before the report is written." },
      { name: "max_searches", label: "Searches", type: "number", for: ["research", "graph"], hint: "Searches in the whole run, before those for empty table cells. Graph: an optional cap on top of the time." },
      { name: "results_per_query", label: "Pages per search", type: "number", for: ["research", "graph"], hint: "Results read from each search." },
      { name: "max_attempts", label: "Attempts per question", type: "number", for: ["research", "graph"], hint: "Differently shaped queries tried before a question is given up." },
      { name: "follow_ups", label: "Follow-up questions", type: "checkbox", for: ["research"], hint: "Whether an answered question can add harder ones." },
      { name: "max_depth", label: "Follow-up depth", type: "number", for: ["research", "graph"], hint: "Levels of follow-ups below the first questions." },
      { name: "recall_past_runs", label: "Use knowledge from past runs", type: "checkbox", for: ["research", "graph"], hint: "Answer from pages earlier runs read, before searching. Off: only this run's own pages are reused; what it reads is still kept for later runs." },
      { name: "max_age_hours", label: "Page cache age (hours)", type: "number", min: 0, for: ["research", "graph"], hint: "How long a saved page is used without asking its site again. 0 downloads every page again and replaces the saved copy." },
    ],
  },
  {
    name: "ranking",
    label: "Ranking",
    title: "Ranking and comparison",
    settings: [
      { name: "max_entities", label: "Most options", type: "number", for: ["research"], hint: "Options compared at most, when the run chooses them." },
      { name: "max_criteria", label: "Most criteria", type: "number", for: ["research"], hint: "Criteria at most, when the run chooses them." },
    ],
  },
  {
    name: "verification",
    label: "Verification",
    title: "Verification",
    settings: [
      { name: "ground", label: "Grounding checker", type: "checkbox", for: ["research", "graph"], hint: "Check each cited sentence against the claims it cites. Needs KVASIR_MODEL_CHECKER." },
      { name: "ground_intake", label: "Ground claims", type: "checkbox", for: ["research", "graph"], hint: "Also check each claim against its page as it is read. Slow when the checker cannot stay loaded beside the fast model." },
      { name: "corroborate", label: "Corroborate", type: "checkbox", for: ["research", "graph"], hint: "Look for other sites supporting each claim." },
      { name: "contradict", label: "Contradictions", type: "checkbox", for: ["research", "graph"], hint: "Look for other sites disagreeing with each claim." },
    ],
  },
  {
    name: "article",
    label: "Article",
    title: "Article",
    settings: [
      { name: "search_top_k", label: "Results per search", type: "number", for: ["article"], hint: "Search results per query." },
      { name: "max_conv_turn", label: "Turns per perspective", type: "number", for: ["article"], hint: "Questions each perspective asks." },
      { name: "max_perspective", label: "Perspectives", type: "number", for: ["article"], hint: "Perspectives researched." },
      { name: "do_polish_article", label: "Polish", type: "checkbox", for: ["article"], hint: "Run the polishing stage. Slower, better prose." },
    ],
  },
  {
    name: "models",
    label: "Models",
    title: "Models",
    settings: [
      { name: "model_fast", label: "Fast model", type: "text", for: ["research", "graph", "article"], hint: "For research and articles. Written provider/name, the provider being one configured on the service." },
      { name: "model_strong", label: "Strong model", type: "text", for: ["research", "graph", "article"], hint: "Outlines and writes research reports, and writes articles. Written provider/name, a provider configured on the service." },
    ],
  },
];

export const SETTINGS = PANES.flatMap((pane) => pane.settings);
export const setting = (name: keyof Defaults) => SETTINGS.find((s) => s.name === name)!;

const KEY = "settings";

// State, so the settings dialog and the new run page show the same values as they change.
export const saved: Partial<Record<keyof Defaults, Value>> = $state(JSON.parse(localStorage.getItem(KEY) ?? "{}"));

// What a run would be sent: the value changed, or else the configured default.
export const value = (name: keyof Defaults, defaults: Partial<Defaults>) => saved[name] ?? defaults[name];

// A value equal to the default is not kept, so a later change of the default reaches it.
export function keep(name: keyof Defaults, value: Value | undefined, defaults: Partial<Defaults>) {
  if (value === undefined || value === defaults[name]) delete saved[name];
  else saved[name] = value;
  localStorage.setItem(KEY, JSON.stringify(saved));
}

export function forget() {
  for (const name of Object.keys(saved) as (keyof Defaults)[]) delete saved[name];
  localStorage.removeItem(KEY);
}

// Only the settings changed for this kind: the rest take the service's defaults.
export function chosen(kind: Kind): Record<string, Value> {
  const request: Record<string, Value> = {};
  for (const setting of SETTINGS) {
    if (setting.name in saved && setting.for.includes(kind)) {
      request[setting.name] = saved[setting.name]!;
    }
  }
  return request;
}
