<script lang="ts">
  import { Dialog, Tabs } from "bits-ui";
  import type { Defaults } from "../lib/api";
  import { forget, PANES } from "../lib/settings.svelte";
  import Icon from "./Icon.svelte";
  import SettingField from "./SettingField.svelte";

  let { open = $bindable(), defaults }: { open: boolean; defaults: Partial<Defaults> } = $props();

  const ICONS = { research: "search", ranking: "chart", article: "page", models: "chip" } as const;

  let pane = $state("research");
</script>

<Dialog.Root bind:open>
  <Dialog.Portal>
    <Dialog.Overlay class="overlay" />
    <Dialog.Content class="settings">
      <header>
        <Dialog.Title class="title">Settings</Dialog.Title>
        <button type="button" class="action" onclick={forget}>Reset all</button>
        <Dialog.Close class="action">Close</Dialog.Close>
      </header>
      <Tabs.Root bind:value={pane} orientation="vertical" class="layout">
        <Tabs.List class="tabs" aria-label="Setting categories">
          {#each PANES as { name, label } (name)}
            <Tabs.Trigger value={name}><Icon name={ICONS[name as keyof typeof ICONS]} />{label}</Tabs.Trigger>
          {/each}
        </Tabs.List>
        <div class="panes">
          {#each PANES as { name, title, settings } (name)}
            <Tabs.Content value={name} class="pane">
              <h3>{title}</h3>
              {#each settings as setting (setting.name)}
                <SettingField {setting} {defaults} />
              {/each}
            </Tabs.Content>
          {/each}
        </div>
      </Tabs.Root>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
