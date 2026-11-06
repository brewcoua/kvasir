// The service's own settings, as opposed to the per-run ones in `settings.svelte.ts`.
import { json, type ModelInfo, type ProviderInfo } from "./api";

export const capitalize = (text: string) => text[0].toUpperCase() + text.slice(1);

// `research.max_searches` as "Max searches".
export const labelOf = (key: string) => capitalize(key.slice(key.indexOf(".") + 1).replaceAll("_", " "));

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
