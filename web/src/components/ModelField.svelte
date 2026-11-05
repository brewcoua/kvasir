<script lang="ts">
  import { Combobox } from "bits-ui";
  import type { ModelInfo } from "../lib/api";
  import { describeModel } from "../lib/service";

  // Takes any text as well as the models listed: a provider that cannot list its models can still
  // be typed. `oncommit` is when the choice is made: picked from the list, or left after typing.
  let {
    id,
    value = $bindable(),
    models,
    required = false,
    oncommit,
  }: { id: string; value: string; models: ModelInfo[]; required?: boolean; oncommit?: () => void } = $props();

  let open = $state(false);
  // Every model until something is typed, so a filled field still offers the others.
  let typed = $state(false);
  let changed = false;

  const shown = $derived(typed ? models.filter((model) => model.ref.toLowerCase().includes(value.trim().toLowerCase())) : models);

  function commit() {
    if (!changed) return;
    changed = false;
    oncommit?.();
  }
</script>

<Combobox.Root
  type="single"
  allowDeselect={false}
  bind:open
  inputValue={value}
  value={models.some((model) => model.ref === value) ? value : ""}
  onValueChange={(ref) => {
    value = ref;
    changed = true;
  }}
  onOpenChange={(isOpen) => {
    typed = false;
    if (!isOpen) commit();
  }}
>
  <div class="model-field">
    <Combobox.Input
      {id}
      type="text"
      autocomplete="off"
      {required}
      oninput={(event) => {
        value = event.currentTarget.value;
        typed = true;
        changed = true;
      }}
      onclick={() => (open = true)}
      onblur={() => {
        // An open list ends by closing, unless nothing matches and so nothing is there to dismiss.
        if (open && shown.length > 0) return;
        open = false;
        typed = false;
        commit();
      }}
    />
    {#if models.length > 0}
      <Combobox.Trigger class="open" aria-label="Show the models">&#9662;</Combobox.Trigger>
    {/if}
  </div>
  {#if shown.length > 0}
    <Combobox.Portal>
      <Combobox.Content class="menu models" sideOffset={4}>
        {#each shown as model (model.ref)}
          <Combobox.Item value={model.ref} label={model.ref} class="option">
            {model.ref}
            {#if describeModel(model)}<span class="meta">{describeModel(model)}</span>{/if}
          </Combobox.Item>
        {/each}
      </Combobox.Content>
    </Combobox.Portal>
  {/if}
</Combobox.Root>
