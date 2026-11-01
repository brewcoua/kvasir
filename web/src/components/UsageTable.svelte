<script lang="ts">
  import type { ModelUsage } from "../lib/api";
  import UsageCells from "./UsageCells.svelte";

  let { title, rows }: { title: string; rows: Record<string, ModelUsage> } = $props();
</script>

<div class="scroll">
  <table>
    <thead>
      <tr>
        <th>{title}</th>
        <th class="n">calls</th><th class="n">in</th><th class="n">out</th><th class="n">reasoning</th><th class="n">cost</th>
        <th class="n">time</th><th class="n">tok/s</th><th class="n">per call</th>
      </tr>
    </thead>
    <tbody>
      {#each Object.entries(rows) as [name, usage] (name)}
        <tr><td>{name}</td><UsageCells {usage} /></tr>
      {/each}
    </tbody>
  </table>
</div>
