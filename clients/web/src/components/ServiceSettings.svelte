<script lang="ts">
  import { onMount } from "svelte";
  import { json, type ModelInfo, type SettingInfo, type SettingsView } from "../lib/api";
  import { capitalize, groupSearch, loadModels } from "../lib/service";
  import ServiceField from "./ServiceField.svelte";

  let view: SettingsView | null = $state(null);
  let models: ModelInfo[] = $state([]);
  let error = $state("");

  // By section, the part of a key before its dot, in the order the service lists them.
  const sections = $derived.by(() => {
    const found = new Map<string, SettingInfo[]>();
    for (const field of view?.fields ?? []) {
      const section = field.key.slice(0, field.key.indexOf("."));
      found.set(section, [...(found.get(section) ?? []), field]);
    }
    return [...found];
  });

  onMount(() => {
    json<SettingsView>("/v1/settings").then((loaded) => (view = loaded), (failure) => (error = String(failure)));
    loadModels().then((found) => (models = found.models), (failure) => console.warn("no models:", failure));
  });
</script>

<h3>Service</h3>
<p class="hint">For every run and every browser, saved as soon as it changes.</p>
{#if error}<p class="error" role="alert">{error}</p>{/if}
{#if view && view.restart_required.length > 0}
  <p class="hint" role="status">Waiting for a restart: {view.restart_required.join(", ")}.</p>
{/if}
{#each sections as [section, fields] (section)}
  <h4>{capitalize(section)}</h4>
  {#if section === "search"}
    {@const search = groupSearch(fields)}
    {#each search.rest as field (field.key)}
      <ServiceField {field} {models} onsaved={(saved) => (view = saved)} />
    {/each}
    {#each search.groups as group (group.title)}
      <h5>{group.title}</h5>
      {#if group.notice}<p class="hint">{group.notice}</p>{/if}
      {#each group.providers as provider (provider.id)}
        {#if provider.price}<p class="hint">{provider.name}: {provider.price}.</p>{/if}
        {#each provider.fields as field (field.key)}
          <ServiceField {field} {models} onsaved={(saved) => (view = saved)} />
        {/each}
      {/each}
    {/each}
  {:else}
    {#each fields as field (field.key)}
      <ServiceField {field} {models} onsaved={(saved) => (view = saved)} />
    {/each}
  {/if}
{/each}
