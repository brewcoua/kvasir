<script lang="ts">
  import type { RunSummary } from "../lib/api";
  import { count } from "../lib/format";
  import { href } from "../lib/route.svelte";
  import Bar from "../components/Bar.svelte";
  import Clock from "../components/Clock.svelte";

  let { runs, summary }: { runs: RunSummary[]; summary: string } = $props();

  const started = (at: number) =>
    new Date(at * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
</script>

<section class="panel">
  <h2>Runs <span class="meta">{summary}</span></h2>
  {#if runs.length === 0}
    <p class="empty">Nothing has run yet. <a href="#/">Start one.</a></p>
  {:else}
    <div class="body scroll">
      <table class="runs">
        <thead>
          <tr>
            <th>run</th><th>state</th><th>kind</th><th>started</th>
            <th class="n">elapsed</th><th class="n">tokens</th><th class="n">cost</th>
          </tr>
        </thead>
        <tbody>
          {#each runs as run (run.id)}
            <tr>
              <td class="topic-cell">
                <a href={href({ page: "run", id: run.id, tab: null })} class="topic">{run.topic}</a>
                {#if run.state === "running"}<Bar event={run.latest} bare />{/if}
              </td>
              <td><span class="state {run.state}">{run.state}</span>{#if run.state === "running" && run.stage}<span class="meta">{" · "}{run.stage}</span>{/if}</td>
              <td>{run.kind}</td>
              <td class="meta">{started(run.created_at)}</td>
              <td class="n"><Clock from={run.started_at} to={run.ended_at} /></td>
              <td class="n">{count(run.usage.prompt_tokens + run.usage.completion_tokens)}</td>
              <td class="n">{run.usage.cost.toFixed(4)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
