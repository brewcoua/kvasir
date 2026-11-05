// Types are generated from the service's OpenAPI schema by `pnpm types`, never written by hand.
import type { components } from "./schema";

type Schemas = components["schemas"];
export type RunSummary = Schemas["RunSummary"];
export type RunDetail = Schemas["RunDetail"];
// The `status` event: a detail without its events and result. Not in the schema, being SSE only.
export type RunStatus = Omit<RunDetail, "events" | "result">;
export type RunResult = NonNullable<RunDetail["result"]>;
export type Progress = Schemas["Progress"];
export type ModelUsage = Schemas["ModelUsage"];
export type LmCall = Schemas["LmCall"];
export type LmCallContent = Schemas["LmCallContent"];
export type SearchCall = Schemas["SearchCall"];
export type SearchContent = Schemas["SearchContent"];
export type ResearchQuestion = Schemas["ResearchQuestion"];
export type ResearchClaim = Schemas["ResearchClaim"];
export type Citation = Schemas["Citation"];
export type SourceInfo = Schemas["SourceInfo"];
export type ResearchResult = Schemas["ResearchResult"];
export type MethodInfo = Schemas["MethodInfo"];
export type Site = Schemas["Site"];
export type SiteGraph = Schemas["SiteGraph"];
export type CacheStats = Schemas["CacheStats"];
export type PageSummary = Schemas["PageSummary"];
export type PageDetail = Schemas["PageDetail"];
export type Recall = Schemas["Recall"];
// The options with a configured default, every method's together. Their names do not collide, but
// for the models, which mean the same in every method.
type Configured = Omit<Schemas["LedgerOptions"] & Schemas["GraphOptions"] & Schemas["StormOptions"], "output" | "criteria" | "entities">;
export type Defaults = { [K in keyof Configured]-?: NonNullable<Configured[K]> };

// `GET /v1/methods` puts each configured default in its option's schema.
export const defaultsOf = (method: MethodInfo): Partial<Defaults> =>
  Object.fromEntries(
    Object.entries((method.options.properties ?? {}) as Record<string, { default?: unknown }>).map(([name, option]) => [
      name,
      option.default,
    ]),
  ) as Partial<Defaults>;

// The token expired or changed: starting the page over shows the login.
export function reloadOn401(response: Response): Response {
  if (response.status === 401) location.reload();
  return response;
}

export async function json<T>(path: string): Promise<T> {
  const response = reloadOn401(await fetch(path));
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

// Like `json`, with the reason the service gives for a refusal, and for requests that change something.
export async function request<T>(method: "GET" | "POST" | "PUT" | "DELETE", path: string, body?: unknown): Promise<T> {
  const response = reloadOn401(
    await fetch(
      path,
      body === undefined ? { method } : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    ),
  );
  if (!response.ok) {
    const failure = await response.json().catch(() => ({}));
    throw new Error(failure.message ?? `${path}: ${response.status}`);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

// One field holds the main text, whichever kind of run produced it.
export function resultText(result: RunResult): string {
  if ("article" in result) return result.article;
  if ("report" in result) return result.report;
  if ("utterance" in result) return result.utterance;
  return JSON.stringify(result, null, 2);
}

export const isResearch = (result: RunResult): result is ResearchResult => "claims" in result;
