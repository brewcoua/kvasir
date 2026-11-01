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
export type Citation = Schemas["Citation"];
export type ResearchResult = Schemas["ResearchResult"];
export type Defaults = Schemas["Defaults"];

export async function json<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

// One field holds the main text, whichever kind of run produced it.
export function resultText(result: RunResult): string {
  if ("article" in result) return result.article;
  if ("report" in result) return result.report;
  if ("utterance" in result) return result.utterance;
  return JSON.stringify(result, null, 2);
}

export const isResearch = (result: RunResult): result is ResearchResult => "claims" in result;
