<script lang="ts">
  import { tick } from "svelte";
  import { request, type SettingInfo, type SettingsView } from "../lib/api";
  import { labelOf } from "../lib/service";

  let { field, modelList, onsaved }: { field: SettingInfo; modelList: string; onsaved: (view: SettingsView) => void } = $props();

  const uid = $props.id();
  const id = $derived(`${uid}-${field.key}`);
  const label = $derived(labelOf(field.key));
  let error = $state("");
  let control: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | undefined = $state();

  // What the service holds, which a refused value or a reset to the same default would not rewrite.
  function show() {
    if (control instanceof HTMLInputElement && control.type === "checkbox") control.checked = Boolean(field.value);
    else if (control) control.value = field.type === "list" ? (field.value as string[]).join("\n") : String(field.value ?? "");
  }

  async function send(method: "PATCH" | "DELETE", path: string, body?: unknown) {
    error = "";
    try {
      onsaved(await request<SettingsView>(method, path, body));
    } catch (failure) {
      error = failure instanceof Error ? failure.message : String(failure);
    }
    await tick();
    show();
  }

  const save = (value: unknown) => send("PATCH", "/v1/settings", { [field.key]: value });
  const reset = () => send("DELETE", `/v1/settings/${encodeURIComponent(field.key)}`);

  function number(input: HTMLInputElement) {
    // An out of range number stays on screen, marked invalid by the browser, and is not sent.
    if (input.value.trim() === "") reset();
    else if (input.reportValidity()) save(Number(input.value));
  }

  // Empty turns a nullable setting off, and gives any other its default. A required one has none.
  function text(input: HTMLInputElement) {
    const value = input.value.trim();
    if (value !== "") save(value);
    else if (field.nullable) save("");
    else if (!field.required) reset();
    else {
      error = `${label} is required: nothing can run without it.`;
      show();
    }
  }

  const lines = (text: string) => text.split("\n").map((line) => line.trim()).filter(Boolean);
</script>

<div class="setting" class:wide={field.type !== "integer" && field.type !== "number" && field.type !== "boolean"} class:changed={field.stored}>
  <label for={id}>{label}</label>
  {#if field.type === "boolean"}
    <input {id} bind:this={control} type="checkbox" checked={Boolean(field.value)} onchange={(e) => save(e.currentTarget.checked)} />
  {:else if field.type === "integer" || field.type === "number"}
    <input
      {id}
      bind:this={control}
      type="number"
      min={field.minimum}
      step={field.type === "number" ? "any" : 1}
      value={field.value}
      onchange={(e) => number(e.currentTarget)}
    />
  {:else if field.type === "list"}
    <textarea {id} bind:this={control} rows="3" value={(field.value as string[]).join("\n")} onchange={(e) => save(lines(e.currentTarget.value))}></textarea>
  {:else if field.choices}
    <select {id} bind:this={control} value={field.value} onchange={(e) => save(e.currentTarget.value)}>
      {#each field.choices as choice (choice)}<option value={choice}>{choice}</option>{/each}
    </select>
  {:else}
    <input
      {id}
      bind:this={control}
      type="text"
      list={field.type === "model" ? modelList : undefined}
      value={field.value ?? ""}
      onchange={(e) => text(e.currentTarget)}
    />
  {/if}
  <button type="button" class="reset" aria-label={`Reset ${label.toLowerCase()} to the default`} onclick={reset}>Reset</button>
  <p class="hint">
    {field.help}
    {#if field.type === "list"}One per line{#if field.choices}, of {field.choices.join(", ")}{/if}.{/if}
    {#if field.restart}Applies at the next restart.{/if}
  </p>
  {#if error}<p class="hint error" role="alert">{error}</p>{/if}
</div>
