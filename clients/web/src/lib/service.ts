// The service's own settings, as opposed to the per-run ones in `settings.svelte.ts`.
import { json, type ModelInfo, type ProviderInfo } from "./api";

const capitalize = (text: string) => text[0].toUpperCase() + text.slice(1);

// `research.max_searches` as "Max searches", `search.brave.api_key` as "Brave api key".
export const labelOf = (key: string) => capitalize(key.slice(key.indexOf(".") + 1).replaceAll(/[._]/g, " "));

// Names that say more than the key does, in the settings dialog.
const LABELS: Record<string, string> = {
  "research.max_searches": "Searches",
  "research.results_per_query": "Pages per search",
  "research.max_attempts": "Attempts per question",
  "research.follow_ups": "Follow-up questions",
  "research.max_depth": "Follow-up depth",
  "research.max_entities": "Most options",
  "research.max_criteria": "Most criteria",
  "storm.max_conv_turn": "Turns per perspective",
  "storm.max_perspective": "Perspectives",
  "search.top_k": "Results per search",
  "fetch.max_age_hours": "Page cache age (hours)",
};
export const settingLabel = (key: string) => LABELS[key] ?? labelOf(key);

export const SCRAPING_NOTICE = "These read each engine's own pages, which is against its terms: turning one on is your choice and your risk. An engine may block them or change its pages without notice.";

// Prices as `docs/search-plan.md` found them. An engine's own page is the authority.
export const SEARCH_GROUPS: { title: string; notice?: string; providers: { id: string; name: string; price?: string }[] }[] = [
  {
    title: "Web, with a key",
    providers: [
      { id: "brave", name: "Brave", price: "$5 per 1,000 queries, $5 a month free" },
      { id: "tavily", name: "Tavily", price: "$8 per 1,000 queries, 1,000 a month free" },
      { id: "exa", name: "Exa", price: "$4 to $7 per 1,000 queries, $10 a month free" },
      { id: "mojeek", name: "Mojeek", price: "£2 per 1,000 queries" },
    ],
  },
  { title: "Web, without a key", providers: [{ id: "searxng", name: "SearXNG" }] },
  {
    title: "Web, scraped",
    notice: SCRAPING_NOTICE,
    providers: [
      { id: "duckduckgo", name: "DuckDuckGo" },
      { id: "bing", name: "Bing" },
      { id: "startpage", name: "Startpage" },
      { id: "mojeek_html", name: "Mojeek HTML" },
    ],
  },
  {
    title: "Specialized",
    providers: [
      { id: "wikipedia", name: "Wikipedia" },
      { id: "arxiv", name: "arXiv" },
      { id: "crossref", name: "Crossref" },
      { id: "europe_pmc", name: "Europe PMC" },
      { id: "semantic_scholar", name: "Semantic Scholar" },
      { id: "openalex", name: "OpenAlex", price: "$1 per 1,000 queries, $1 a day free" },
    ],
  },
];

// A provider that cannot list its models is told apart, so a page can say which.
export async function loadModels(): Promise<{ models: ModelInfo[]; unreachable: string[]; providers: number }> {
  const providers = await json<ProviderInfo[]>("/v1/providers");
  const lists = await Promise.all(
    providers
      .filter((provider) => provider.reachable)
      .map((provider) => json<ModelInfo[]>(`/v1/providers/${encodeURIComponent(provider.name)}/models`).catch(() => [])),
  );
  return {
    models: lists.flat(),
    unreachable: providers.filter((provider) => !provider.reachable).map((provider) => provider.name),
    providers: providers.length,
  };
}

// Listed under its ref in a model field. The window is the one trained with, not the one served.
export function describeModel(model: ModelInfo): string {
  const parts = [...(model.capabilities ?? [])];
  if (model.context_length) parts.push(`trained window ${model.context_length}`);
  return parts.join(", ");
}
