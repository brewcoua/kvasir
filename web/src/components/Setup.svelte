<script lang="ts">
  import { onMount } from "svelte";
  import { request, type ModelInfo, type SettingsView } from "../lib/api";
  import { describeModel, labelOf, loadModels } from "../lib/service";
  import ModelList from "./ModelList.svelte";

  const MODEL_LIST = "setup-models";
  const EMBEDDING = "model.embedding";

  let { view, ondone }: { view: SettingsView; ondone: () => void } = $props();

  // What a run cannot do without. Everything else has a default, in the settings dialog. Read once:
  // what is typed here replaces it.
  // svelte-ignore state_referenced_locally
  const fields = view.fields.filter((field) => field.required);
  const values: Record<string, string> = $state(Object.fromEntries(fields.map((field) => [field.key, String(field.value ?? "")])));

  let models: ModelInfo[] = $state([]);
  let unreachable: string[] = $state([]);
  let noProviders = $state(false);
  let error = $state("");
  let busy = $state(false);
  // What did not answer once saved. Saved all the same: it may only be starting.
  let silent: string[] | null = $state(null);

  onMount(() => {
    loadModels().then(
      (found) => {
        models = found.models;
        unreachable = found.unreachable;
        noProviders = found.providers === 0;
        // Only the embedding model can be told from a list: nothing says which is fast or strong.
        values[EMBEDDING] ||=
          models.find((model) => model.capabilities?.includes("embedding"))?.ref ??
          models.find((model) => model.id.toLowerCase().includes("embed"))?.ref ??
          "";
      },
      (failure) => (error = String(failure)),
    );
  });

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    busy = true;
    error = "";
    silent = null;
    try {
      const entered = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, value.trim()]));
      const saved = await request<SettingsView>("PATCH", "/v1/settings", entered);
      if (saved.missing.length > 0) {
        error = `Still unset: ${saved.missing.join(", ")}.`;
        return;
      }
      // 503 is an answer here, so not through `request`.
      const ready = await (await fetch("/readyz")).json();
      const failed = [
        ...Object.entries(ready.providers ?? {}).filter(([, ok]) => !ok).map(([name]) => `provider ${name}`),
        ...(ready.searxng === false ? ["SearXNG"] : []),
      ];
      if (failed.length === 0) ondone();
      else silent = failed;
    } catch (failure) {
      error = failure instanceof Error ? failure.message : String(failure);
    } finally {
      busy = false;
    }
  }
</script>

<form class="panel setup" onsubmit={submit}>
  <h2>Set up kvasir</h2>
  <p class="meta">Nothing can run without these. The rest is under Settings, once this is done.</p>
  {#if noProviders}
    <p class="error" role="alert">
      No provider is configured. Set <code>KVASIR_PROVIDER_&lt;NAME&gt;_API_BASE</code> and restart the service.
    </p>
  {/if}
  {#if unreachable.length > 0}
    <p class="meta" role="status">Did not list its models: {unreachable.join(", ")}. Its models can still be typed, as <code>provider/name</code>.</p>
  {/if}
  <ModelList id={MODEL_LIST} {models} />
  {#each fields as field (field.key)}
    {@const chosen = models.find((model) => model.ref === values[field.key])}
    <label for={field.key}>{field.type === "model" ? `${labelOf(field.key)} model` : field.key === "search.url" ? "SearXNG URL" : labelOf(field.key)}</label>
    <input
      id={field.key}
      type="text"
      list={field.type === "model" ? MODEL_LIST : undefined}
      autocomplete="off"
      bind:value={values[field.key]}
      required
    />
    <p class="meta">{field.help}{#if chosen && describeModel(chosen)}{" "}<br />{describeModel(chosen)}{/if}</p>
  {/each}
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if silent}
    <p class="error" role="alert">Saved, but these did not answer: {silent.join(", ")}.</p>
    <button type="button" class="action" onclick={ondone}>Continue anyway</button>
  {/if}
  <button type="submit" class="action" disabled={busy}>Save</button>
</form>
