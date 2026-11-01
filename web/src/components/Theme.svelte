<script lang="ts">
  import { Select } from "bits-ui";

  // `system` follows the OS between light and dark. Every other theme is fixed.
  const THEMES = ["system", "light", "dark", "paper", "riso", "amber", "moss", "navy"];

  let theme = $state(document.documentElement.dataset.theme ?? "system");
  $effect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("theme", theme);
  });
</script>

<div class="meta theme">
  <span id="theme-label">Theme</span>
  <Select.Root type="single" bind:value={theme} items={THEMES.map((name) => ({ value: name, label: name }))}>
    <Select.Trigger class="select" aria-labelledby="theme-label">{theme}</Select.Trigger>
    <Select.Portal>
      <Select.Content class="menu" sideOffset={4}>
        <Select.Viewport>
          {#each THEMES as name (name)}
            <Select.Item value={name} label={name} class="option">{name}</Select.Item>
          {/each}
        </Select.Viewport>
      </Select.Content>
    </Select.Portal>
  </Select.Root>
</div>
