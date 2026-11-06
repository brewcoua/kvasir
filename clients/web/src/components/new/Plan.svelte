<script lang="ts">
  import type { Defaults } from "../../lib/api";
  import { MODELS, values, type Method, type Picked } from "../../lib/methods";
  import { saved, setting, value } from "../../lib/settings.svelte";
  import Fold from "../Fold.svelte";
  import SettingField from "../SettingField.svelte";

  let { method, picked, defaults }: { method: Method; picked: Picked; defaults: Partial<Defaults> } = $props();

  const shown = $derived(values(method, defaults));
  const models = $derived(MODELS.filter((name) => setting(name).for.includes(method.kind)));
</script>

<ol class="plan">
  {#each method.stages as stage (stage.name)}
    {@const skipped = stage.skipped?.(shown, picked) ?? false}
    {@const note = stage.note?.(shown, picked)}
    <li class:skipped>
      <div class="name">{stage.name}{#if note}<span class="meta">{" · "}{note}</span>{/if}</div>
      <p>{stage.does(shown, picked)}</p>
      {#if stage.settings.length > 0}
        {@const changed = stage.settings.filter((name) => name in saved).length}
        <Fold open={changed > 0} class="meta">
          {#snippet head()}adjust{#if changed}<span class="changed">{" · "}{changed} changed</span>{/if}{/snippet}
          {#each stage.settings as name (name)}<SettingField setting={setting(name)} {defaults} />{/each}
        </Fold>
      {/if}
    </li>
  {/each}
</ol>
<Fold class="models meta">
  {#snippet head()}models: {models.map((name) => String(value(name, defaults) ?? "").replace(/^openai\//, "")).join(", ")}{/snippet}
  {#each models as name (name)}<SettingField setting={setting(name)} {defaults} />{/each}
</Fold>
