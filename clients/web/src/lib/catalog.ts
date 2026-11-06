// Where each setting sits in the settings dialog. A setting appears once: those the service holds are shown as the
// service's, and only what has no field there is kept in the browser. The new run page still overrides either, per run.
import type { IconName } from "../components/Icon.svelte";
import type { Defaults, SettingInfo } from "./api";
import { SEARCH_GROUPS, settingLabel } from "./service";
import { setting, type Setting } from "./settings.svelte";

export type Item = { local: Setting; service?: undefined } | { service: SettingInfo; local?: undefined };

// A setting is named here by what it is: a service key, `<prefix>.` for every key under it that nothing else claimed,
// or otherwise the name of a browser setting.
interface FoldPlan {
  id: string;
  title: string;
  note?: string;
  fields: string[];
}
interface SectionPlan {
  title?: string;
  notice?: string;
  fields?: string[];
  folds?: FoldPlan[];
}
interface PanePlan {
  id: string;
  label: string;
  icon: IconName;
  sections: SectionPlan[];
}

const providers = (title: string): FoldPlan[] =>
  SEARCH_GROUPS.find((group) => group.title === title)!.providers.map((provider) => ({
    id: provider.id,
    title: provider.name,
    note: provider.price,
    fields: [`search.${provider.id}.`],
  }));

const PLAN: { label: string; panes: PanePlan[] }[] = [
  {
    label: "Runs",
    panes: [
      {
        id: "research",
        label: "Research",
        icon: "search",
        sections: [
          { title: "Budget", fields: ["research_minutes", "research.max_searches", "research.results_per_query", "research.max_attempts"] },
          { title: "Follow-ups", fields: ["research.follow_ups", "research.max_depth"] },
          { title: "Past runs", fields: ["recall_past_runs"] },
        ],
      },
      { id: "ranking", label: "Ranking", icon: "ranking", sections: [{ fields: ["research.max_entities", "research.max_criteria"] }] },
      { id: "verification", label: "Verification", icon: "verify", sections: [{ fields: ["ground", "ground_intake", "corroborate", "contradict"] }] },
      { id: "article", label: "Article", icon: "page", sections: [{ fields: ["storm.max_perspective", "storm.max_conv_turn", "do_polish_article"] }] },
    ],
  },
  {
    label: "Models",
    panes: [
      { id: "roles", label: "Roles", icon: "chip", sections: [{ fields: ["model.fast", "model.strong", "model.reason", "model.embedding", "model.checker"] }] },
      { id: "reasoning", label: "Tuning", icon: "sliders", sections: [{ fields: ["model.reasoning_effort", "model.reason_effort", "model.context_tokens"] }] },
    ],
  },
  {
    label: "Search",
    panes: [
      {
        id: "web",
        label: "Web",
        icon: "globe",
        sections: [
          { fields: ["search.top_k"] },
          { title: "Web, with a key", folds: providers("Web, with a key") },
          { title: "Web, without a key", folds: providers("Web, without a key") },
          { title: "Web, scraped", notice: SEARCH_GROUPS.find((group) => group.title === "Web, scraped")!.notice, folds: providers("Web, scraped") },
        ],
      },
      { id: "specialized", label: "Specialized", icon: "book", sections: [{ folds: providers("Specialized") }] },
    ],
  },
  {
    label: "Service",
    panes: [
      { id: "runs", label: "Run storage", icon: "play", sections: [{ fields: ["runs."] }] },
      { id: "fetching", label: "Fetching", icon: "download", sections: [{ fields: ["fetch."] }] },
      { id: "favicons", label: "Favicons", icon: "image", sections: [{ fields: ["favicons."] }] },
      { id: "sources", label: "Sources", icon: "tag", sections: [{ fields: ["sources."] }] },
    ],
  },
];

export interface Fold {
  id: string;
  title: string;
  note?: string;
  // Whether the provider is on, for one that can be.
  enabled?: boolean;
  items: Item[];
}
export interface Section {
  title?: string;
  notice?: string;
  items: Item[];
  folds: Fold[];
}
export interface Pane {
  id: string;
  label: string;
  icon: IconName;
  sections: Section[];
  // The settings on it kept in the browser, not the service.
  local: Setting[];
}
export interface Group {
  label: string;
  panes: Pane[];
}

const namesOf = (plan: SectionPlan) => [...(plan.fields ?? []), ...(plan.folds ?? []).flatMap((fold) => fold.fields)];

// Empty panes are left out: until the service has answered, only what the browser keeps is known. A field the plan
// does not place goes under Other, so a new one is never out of reach.
export function catalog(fields: SettingInfo[]): Group[] {
  const named = new Set(PLAN.flatMap((group) => group.panes.flatMap((pane) => pane.sections.flatMap(namesOf))));
  const claimed = new Set<string>(fields.filter((field) => named.has(field.key)).map((field) => field.key));
  const items = (names: string[]): Item[] =>
    names.flatMap((name): Item[] => {
      if (name.endsWith(".")) {
        const under = fields.filter((field) => field.key.startsWith(name) && !claimed.has(field.key));
        for (const field of under) claimed.add(field.key);
        return under.map((service) => ({ service }));
      }
      if (!name.includes(".")) return [{ local: setting(name as keyof Defaults) }];
      const service = fields.find((field) => field.key === name);
      return service ? [{ service }] : [];
    });
  const groups: Group[] = PLAN.map(({ label, panes }) => ({
    label,
    panes: panes.map(({ sections, ...pane }) => {
      const built = sections.map((section) => ({
        title: section.title,
        notice: section.notice,
        items: items(section.fields ?? []),
        folds: (section.folds ?? [])
          .map(({ fields: names, ...fold }) => {
            const found = items(names);
            const on = found.find((item) => item.service?.key.endsWith(".enabled"))?.service?.value;
            return { ...fold, items: found, enabled: typeof on === "boolean" ? on : undefined };
          })
          .filter((fold) => fold.items.length > 0),
      }));
      const shown = built.filter((section) => section.items.length + section.folds.length > 0);
      return { ...pane, sections: shown, local: shown.flatMap((section) => section.items.flatMap((item) => (item.local ? [item.local] : []))) };
    }),
  }));
  const rest = fields.filter((field) => !claimed.has(field.key));
  if (rest.length > 0) {
    const other: Pane = { id: "other", label: "Other", icon: "sliders", sections: [{ items: rest.map((service) => ({ service })), folds: [] }], local: [] };
    groups.at(-1)!.panes.push(other);
  }
  return groups.map((group) => ({ ...group, panes: group.panes.filter((pane) => pane.sections.length > 0) })).filter((group) => group.panes.length > 0);
}

export interface Entry {
  key: string;
  label: string;
  help: string;
  pane: string;
  fold?: string;
  // Where it is, for a search result.
  path: string;
}

function entries(groups: Group[]): Entry[] {
  return groups.flatMap((group) =>
    group.panes.flatMap((pane) =>
      pane.sections.flatMap((section) => {
        const at = [group.label, pane.label, ...(section.title ? [section.title] : [])].join(" / ");
        const entry = (item: Item, fold?: Fold): Entry =>
          item.local
            ? { key: item.local.name, label: item.local.label, help: item.local.hint, pane: pane.id, path: at }
            : { key: item.service!.key, label: settingLabel(item.service!.key), help: item.service!.help, pane: pane.id, fold: fold?.id, path: fold ? `${at} / ${fold.title}` : at };
        return [...section.items.map((item) => entry(item)), ...section.folds.flatMap((fold) => fold.items.map((item) => entry(item, fold)))];
      }),
    ),
  );
}

// Every word somewhere in it, those matching a label first.
export function search(groups: Group[], query: string): Entry[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const haystack = (entry: Entry) => `${entry.label} ${entry.help} ${entry.key} ${entry.path}`.toLowerCase();
  const inLabel = (entry: Entry) => words.every((word) => entry.label.toLowerCase().includes(word));
  const hits = entries(groups).filter((entry) => words.every((word) => haystack(entry).includes(word)));
  return [...hits.filter(inLabel), ...hits.filter((entry) => !inLabel(entry))];
}
