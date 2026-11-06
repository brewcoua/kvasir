<script lang="ts">
  import type { RunSummary } from "../../lib/api";
  import { href } from "../../lib/route.svelte";
  import Clock from "../Clock.svelte";

  // Only the method's own runs, so the list shows what a run like this one gave.
  let { runs, label }: { runs: RunSummary[]; label: string } = $props();
</script>

{#if runs.length > 0}
  <section class="recent">
    <h3>Recent {label} <a href="#/runs" class="meta">all runs</a></h3>
    <ul>
      {#each runs.slice(0, 5) as run (run.id)}
        <li>
          <a href={href({ page: "run", id: run.id, tab: null })} class="topic">{run.topic}</a>
          <span class="meta"><span class="state {run.state}">{run.state}</span> · <Clock from={run.started_at} to={run.ended_at} /></span>
        </li>
      {/each}
    </ul>
  </section>
{/if}
