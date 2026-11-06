<script lang="ts">
  import type { RunSummary } from "../lib/api";
  import { count } from "../lib/format";
  import { href } from "../lib/route.svelte";
  import Bar from "./Bar.svelte";
  import Clock from "./Clock.svelte";

  let { runs, selected }: { runs: RunSummary[]; selected: string } = $props();
</script>

<nav class="list" aria-label="Runs">
  {#each runs as run (run.id)}
    {@const tokens = run.usage.prompt_tokens + run.usage.completion_tokens}
    <a href={href({ page: "run", id: run.id, tab: null })} aria-current={run.id === selected ? "page" : undefined}>
      <span class="topic">{run.topic}</span>
      <span class="meta">
        <span class="state {run.state}">{run.state}</span>{#if run.state === "running" && run.stage}{" · "}{run.stage}{/if}
        {" · "}{run.kind}{" · "}<Clock from={run.started_at} to={run.ended_at} />{#if tokens > 0}{" · "}{count(tokens)} tok{/if}
      </span>
      {#if run.state === "running"}<Bar event={run.latest} bare />{/if}
    </a>
  {/each}
</nav>
