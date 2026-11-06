import { describe, expect, it } from "vitest";
import type { SettingInfo } from "./api";
import { catalog, search, type Group, type Item } from "./catalog";

const field = (key: string, value: unknown = 1) => ({ key, value, help: `help of ${key}`, type: "integer" }) as unknown as SettingInfo;

const panes = (groups: Group[]) => groups.flatMap((group) => group.panes);
const itemsOf = (groups: Group[]): Item[] => panes(groups).flatMap((pane) => pane.sections.flatMap((section) => [...section.items, ...section.folds.flatMap((fold) => fold.items)]));
const keys = (groups: Group[]) => itemsOf(groups).map((item) => item.service?.key ?? item.local!.name);

const FIELDS = ["model.fast", "research.max_searches", "search.top_k", "search.mojeek.enabled", "search.mojeek_html.enabled", "runs.store", "favicons.cache"].map((key) => field(key));

describe("catalog", () => {
  it("shows a setting the service holds only as the service's", () => {
    const shown = keys(catalog(FIELDS));
    expect(new Set(shown).size).toBe(shown.length);
    for (const name of ["model_fast", "model_strong", "max_searches", "results_per_query", "max_attempts", "follow_ups", "max_depth", "max_entities", "max_criteria", "search_top_k", "max_conv_turn", "max_perspective", "max_age_hours"]) {
      expect(shown).not.toContain(name);
    }
    expect(shown).toContain("research.max_searches");
    expect(shown).toContain("ground");
  });

  it("names what a pane keeps in the browser", () => {
    const pane = (id: string) => panes(catalog(FIELDS)).find((found) => found.id === id)!;
    expect(pane("verification").local.map((setting) => setting.name)).toEqual(["ground", "ground_intake", "corroborate", "contradict"]);
    expect(pane("roles").local).toEqual([]);
  });

  it("leaves out what has nothing to show until the service answers", () => {
    expect(panes(catalog([])).map((pane) => pane.id)).toEqual(["research", "verification", "article"]);
  });

  it("folds a provider's fields apart from one whose name starts the same, and says if it is on", () => {
    const web = panes(catalog([field("search.mojeek.enabled", true), field("search.mojeek.weight"), field("search.mojeek_html.enabled", false)])).find((pane) => pane.id === "web")!;
    const folds = web.sections.flatMap((section) => section.folds);
    expect(folds.map((fold) => [fold.id, fold.items.length, fold.enabled])).toEqual([
      ["mojeek", 2, true],
      ["mojeek_html", 1, false],
    ]);
  });

  it("puts a field nothing places under Other", () => {
    const groups = catalog([field("search.unknown.enabled"), field("brand.new")]);
    const other = panes(groups).find((pane) => pane.id === "other")!;
    expect(other.sections[0].items.map((item) => item.service!.key)).toEqual(["search.unknown.enabled", "brand.new"]);
    expect(panes(catalog(FIELDS)).some((pane) => pane.id === "other")).toBe(false);
  });
});

describe("search", () => {
  const groups = catalog(FIELDS);
  const found = (query: string) => search(groups, query).map((entry) => entry.key);

  it("finds by label, help, key and place, every word", () => {
    expect(found("searches")).toEqual(["research.max_searches"]);
    expect(found("help of runs.store")).toEqual(["runs.store"]);
    expect(found("favicons cache")).toEqual(["favicons.cache"]);
    expect(found("verification ground")).toEqual(["ground", "ground_intake"]);
    expect(found("no such thing")).toEqual([]);
    expect(found("   ")).toEqual([]);
  });

  it("puts a match on the label ahead of one in the text", () => {
    expect(found("ground")[0]).toBe("ground");
  });

  it("says where a provider's field lives, so it can be opened", () => {
    expect(search(groups, "mojeek enabled")[0]).toMatchObject({ pane: "web", fold: "mojeek", path: "Search / Web / Web, with a key / Mojeek" });
  });
});
