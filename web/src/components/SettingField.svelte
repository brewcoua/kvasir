<script lang="ts">
  import type { Defaults } from "../lib/api";
  import { keep, saved, type Setting } from "../lib/settings.svelte";

  let { setting, defaults }: { setting: Setting; defaults: Partial<Defaults> } = $props();

  // Unique per field, as the new run page can show a setting the dialog also shows.
  const uid = $props.id();
  const id = $derived(`${uid}-${setting.name}`);
  const changed = $derived(setting.name in saved);
  const shown = $derived(saved[setting.name] ?? defaults[setting.name]);

  function change(input: HTMLInputElement) {
    if (setting.type === "checkbox") keep(setting.name, input.checked, defaults);
    else if (input.value.trim() === "") keep(setting.name, undefined, defaults);
    // An out of range number stays on screen, marked invalid by the browser, and is not kept.
    else if (!input.checkValidity()) return;
    else keep(setting.name, setting.type === "number" ? Number(input.value) : input.value.trim(), defaults);
  }
</script>

<div class="setting" class:wide={setting.type === "text"} class:changed>
  <label for={id}>{setting.label}</label>
  {#if setting.type === "checkbox"}
    <input {id} type="checkbox" checked={Boolean(shown)} onchange={(e) => change(e.currentTarget)} />
  {:else}
    <input
      {id}
      type={setting.type}
      min={setting.type === "number" ? (setting.min ?? 1) : undefined}
      value={shown ?? ""}
      placeholder={String(defaults[setting.name] ?? "")}
      onchange={(e) => change(e.currentTarget)}
    />
  {/if}
  <button type="button" class="reset" aria-label={`Reset ${setting.label.toLowerCase()} to the default`} onclick={() => keep(setting.name, undefined, defaults)}>
    Reset
  </button>
  <p class="hint">{setting.hint}</p>
</div>
