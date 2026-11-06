<script lang="ts">
  import { Accordion, Command, Dialog, Tabs } from "bits-ui";
  import { tick } from "svelte";
  import { json, type Defaults, type ModelInfo, type SettingsView } from "../lib/api";
  import { catalog, search, type Entry, type Item } from "../lib/catalog";
  import { loadModels } from "../lib/service";
  import { forget } from "../lib/settings.svelte";
  import Icon from "./Icon.svelte";
  import ServiceField from "./ServiceField.svelte";
  import SettingField from "./SettingField.svelte";

  let {
    open = $bindable(),
    defaults,
    onsaved,
  }: {
    open: boolean;
    defaults: Partial<Defaults>;
    // A service setting changed, which can be the default of a run option.
    onsaved: () => void;
  } = $props();

  let pane = $state("research");
  let folds: string[] = $state([]);
  let query = $state("");
  let view = $state<SettingsView | null>(null);
  let models: ModelInfo[] = $state([]);
  let error = $state("");
  let input: HTMLInputElement | null = $state(null);

  const groups = $derived(catalog(view?.fields ?? []));
  const current = $derived(groups.flatMap((group) => group.panes).find((found) => found.id === pane));
  const found = $derived(search(groups, query));

  $effect(() => {
    if (!open) return;
    query = "";
    error = "";
    json<SettingsView>("/v1/settings").then((loaded) => (view = loaded), (failure) => (error = String(failure)));
    loadModels().then((listed) => (models = listed.models), (failure) => console.warn("no models:", failure));
  });

  function saved(next: SettingsView) {
    view = next;
    onsaved();
  }

  async function jump(entry: Entry) {
    query = "";
    pane = entry.pane;
    if (entry.fold && !folds.includes(entry.fold)) folds = [...folds, entry.fold];
    await tick();
    const row = document.querySelector<HTMLElement>(`.settings [data-setting="${entry.key}"]`);
    row?.scrollIntoView({ block: "center" });
    row?.querySelector<HTMLElement>("input, textarea, [role='switch'], button:not(.reset)")?.focus();
    row?.classList.add("found");
  }
</script>

{#snippet field(item: Item)}
  {#if item.local}
    <SettingField setting={item.local} {defaults} />
  {:else}
    <ServiceField field={item.service!} {models} onsaved={saved} />
  {/if}
{/snippet}

<Dialog.Root bind:open>
  <Dialog.Portal>
    <Dialog.Overlay class="overlay" />
    <Dialog.Content class="settings" onOpenAutoFocus={(event) => (event.preventDefault(), input?.focus())}>
      <header>
        <Dialog.Title class="title">Settings</Dialog.Title>
        <Dialog.Close class="action">Close</Dialog.Close>
      </header>
      <Command.Root shouldFilter={false} label="Search settings" class="settings-search">
        <div class="search-box">
          <Icon name="search" />
          <Command.Input bind:ref={input} bind:value={query} placeholder="Search settings" aria-label="Search settings" />
        </div>
        {#if query.trim()}
          <Command.List class="results">
            {#each found as entry (entry.key)}
              <Command.Item value={entry.key} class="setting-hit" onSelect={() => jump(entry)}>
                <span class="setting-hit-label">{entry.label}</span>
                <span class="meta">{entry.path}</span>
                <span class="hint">{entry.help}</span>
              </Command.Item>
            {/each}
            {#if found.length === 0}<p class="meta">No setting matches.</p>{/if}
          </Command.List>
        {/if}
      </Command.Root>
      {#if !query.trim()}
        <Tabs.Root bind:value={pane} orientation="vertical" class="layout">
          <Tabs.List class="tabs" aria-label="Setting categories">
            {#each groups as group (group.label)}
              <div class="tab-group" role="presentation">{group.label}</div>
              {#each group.panes as { id, label, icon } (id)}
                <Tabs.Trigger value={id}><Icon name={icon} />{label}</Tabs.Trigger>
              {/each}
            {/each}
          </Tabs.List>
          <div class="panes">
            {#if error}<p class="error" role="alert">{error}</p>{/if}
            {#if view && view.restart_required.length > 0}
              <p class="hint restart" role="status">Waiting for a restart: {view.restart_required.join(", ")}.</p>
            {/if}
            {#if current}
              <Tabs.Content value={current.id} class="pane">
                <h3>{current.label}</h3>
                {#if current.local.length > 0}
                  <p class="hint">
                    Kept in this browser: {current.local.map((setting) => setting.label).join(", ")}.
                    <button type="button" class="link" onclick={forget}>Reset what this browser keeps</button>
                  </p>
                {/if}
                {#each current.sections as section, i (section.title ?? i)}
                  {#if section.title}<h4>{section.title}</h4>{/if}
                  {#if section.notice}<p class="hint">{section.notice}</p>{/if}
                  {#each section.items as item (item.service?.key ?? item.local?.name)}
                    {@render field(item)}
                  {/each}
                  {#if section.folds.length > 0}
                    <Accordion.Root type="multiple" bind:value={folds} class="folds">
                      {#each section.folds as fold (fold.id)}
                        <Accordion.Item value={fold.id} class="fold">
                          <Accordion.Header level={5}>
                            <Accordion.Trigger class="fold-head">
                              <Icon name="chevron" />
                              <span class="fold-title">{fold.title}</span>
                              {#if fold.note}<span class="meta">{fold.note}</span>{/if}
                              {#if fold.enabled}<span class="chip done">on</span>{/if}
                            </Accordion.Trigger>
                          </Accordion.Header>
                          <Accordion.Content class="fold-body">
                            {#each fold.items as item (item.service?.key ?? item.local?.name)}
                              {@render field(item)}
                            {/each}
                          </Accordion.Content>
                        </Accordion.Item>
                      {/each}
                    </Accordion.Root>
                  {/if}
                {/each}
              </Tabs.Content>
            {/if}
          </div>
        </Tabs.Root>
      {/if}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
