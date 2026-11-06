<script lang="ts">
  import { Switch } from "bits-ui";
  import { tick } from "svelte";
  import { request, type ModelInfo, type SettingInfo, type SettingsView } from "../lib/api";
  import { settingLabel } from "../lib/service";
  import Choice from "./Choice.svelte";
  import ListField from "./ListField.svelte";
  import ModelField from "./ModelField.svelte";

  let { field, models, onsaved }: { field: SettingInfo; models: ModelInfo[]; onsaved: (view: SettingsView) => void } = $props();

  const uid = $props.id();
  const id = $derived(`${uid}-${field.key}`);
  const label = $derived(settingLabel(field.key));
  let error = $state("");
  let control: HTMLInputElement | undefined = $state();
  // svelte-ignore state_referenced_locally
  let model = $state(String(field.value ?? ""));

  // What the service holds, which a refused value or a reset to the same default would not rewrite.
  function show() {
    model = String(field.value ?? "");
    if (control && field.type === "secret") control.value = "";
    else if (control) control.value = String(field.value ?? "");
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
  function text(typed: string) {
    const value = typed.trim();
    if (value !== "") save(value);
    else if (field.nullable) save("");
    else if (!field.required) reset();
    else {
      error = `${label} is required: nothing can run without it.`;
      show();
    }
  }

  // Empty keeps what is stored, since the field never shows it. Reset removes it.
  function secret(typed: string) {
    const value = typed.trim();
    if (value !== "") save(value);
  }
</script>

<div class="setting" data-setting={field.key} class:wide={field.type !== "integer" && field.type !== "number" && field.type !== "boolean"} class:changed={field.stored}>
  <label for={id}>{label}</label>
  {#if field.type === "boolean"}
    <Switch.Root {id} class="switch" bind:checked={() => Boolean(field.value), (on) => save(on)}>
      <Switch.Thumb class="switch-thumb" />
    </Switch.Root>
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
  {:else if field.type === "secret"}
    <input
      {id}
      bind:this={control}
      type="password"
      autocomplete="new-password"
      placeholder={(field.value as { set: boolean }).set ? "set" : "not set"}
      onchange={(e) => secret(e.currentTarget.value)}
    />
  {:else if field.type === "list"}
    <ListField {id} items={field.value as string[]} choices={field.choices ?? undefined} onchange={save} />
  {:else if field.choices}
    <Choice {id} value={String(field.value)} choices={field.choices} onchange={save} />
  {:else if field.type === "model"}
    <ModelField {id} bind:value={model} {models} oncommit={() => text(model)} />
  {:else}
    <input {id} bind:this={control} type="text" value={field.value ?? ""} onchange={(e) => text(e.currentTarget.value)} />
  {/if}
  <button type="button" class="reset" aria-label={`Reset ${label.toLowerCase()} to the default`} onclick={reset}>Reset</button>
  <p class="hint">
    {field.help}
    {#if field.restart}Applies at the next restart.{/if}
  </p>
  {#if error}<p class="hint error" role="alert">{error}</p>{/if}
</div>
