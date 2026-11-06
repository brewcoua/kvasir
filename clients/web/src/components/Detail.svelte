<script lang="ts">
  import { Tabs } from "bits-ui";
  import type { RunDetail } from "../lib/api";
  import { clock } from "../lib/clock.svelte";
  import { count, duration } from "../lib/format";
  import { meter, overall, skipped, stageCounter } from "../lib/steps";
  import Calls from "./Calls.svelte";
  import Clock from "./Clock.svelte";
  import Meter from "./Meter.svelte";
  import Graph from "./Graph.svelte";
  import Result from "./Result.svelte";
  import Searches from "./Searches.svelte";
  import Steps from "./Steps.svelte";
  import UsageTable from "./UsageTable.svelte";

  let { run, tab, ontab }: { run: RunDetail; tab: string; ontab: (tab: string) => void } = $props();

  // The event the calls and searches tabs are filtered to.
  let event: number | null = $state(null);

  const usage = $derived(run.usage);
  const live = $derived(run.ended_at === null);

  // Planned stages in order, then any the plan did not name, so a run shows what it saw. A
  // planned stage not reached yet has an empty track.
  const rows = $derived.by(() => {
    const start = run.started_at ?? 0;
    const end = run.ended_at ?? clock.now;
    const width = Math.max(end - start, 0.001);
    const seen = run.stages.map((stage) => stage.name);
    const names = [...run.planned_stages, ...seen.filter((name) => !run.planned_stages.includes(name))];
    return names.map((name) => {
      const stage = run.stages.find((s) => s.name === name);
      return {
        name,
        state: name === run.stage && live ? "current" : stage ? "" : skipped(run, name) ? "skipped" : "pending",
        seconds: stage?.seconds ?? null,
        spans: (stage?.spans ?? []).map((span) => {
          const left = ((span.started_at - start) / width) * 100;
          const right = (((span.ended_at ?? end) - start) / width) * 100;
          return { left, width: Math.max(right - left, 0), open: span.ended_at === null };
        }),
      };
    });
  });

  const linked = $derived.by(() => {
    const counts = new Map<number, number>();
    for (const call of run.calls) if (call.event != null) counts.set(call.event, (counts.get(call.event) ?? 0) + 1);
    return counts;
  });

  function showCalls(seq: number) {
    event = seq;
    ontab("calls");
  }

  let events: HTMLUListElement | undefined = $state();
  $effect(() => {
    void run.events.length;
    if (events) events.scrollTop = events.scrollHeight;
  });

  // The whole run on the top half of the bar, the current stage's count on the bottom half.
  const whole = $derived(overall(run));
  const part = $derived.by(() => {
    const counter = stageCounter(run);
    return counter ? meter(counter, clock.now, live) : null;
  });

  const offset = (at: number, digits: number) =>
    run.started_at ? `+${(at - run.started_at).toFixed(digits)}s` : "";
</script>

<p class="heading"><span class="state {run.state}">{run.state}</span> · {run.kind} · {run.topic}</p>

{#if run.error}<p class="error">{run.error}</p>{/if}

<Tabs.Root value={tab} onValueChange={ontab}>
  <Tabs.List class="views" aria-label="Run views">
    {#if run.result}<Tabs.Trigger value="result">Result</Tabs.Trigger>{/if}
    <Tabs.Trigger value="overview">Overview</Tabs.Trigger>
    <Tabs.Trigger value="steps">Steps</Tabs.Trigger>
    <Tabs.Trigger value="usage">Usage</Tabs.Trigger>
    <Tabs.Trigger value="calls">Calls <span class="meta">{count(run.calls.length)}</span></Tabs.Trigger>
    <Tabs.Trigger value="searches">Searches <span class="meta">{count(run.searches.length)}</span></Tabs.Trigger>
    {#if run.kind === "graph"}<Tabs.Trigger value="graph">Graph</Tabs.Trigger>{/if}
  </Tabs.List>

  {#if run.result}
    <Tabs.Content value="result"><Result result={run.result} /></Tabs.Content>
  {/if}

  <Tabs.Content value="overview">
    {#if run.latest && live}
      <p class="now"><span class="stage">{run.latest.stage}</span> {run.latest.detail}</p>
      <div class="split-bar" data-testid="split-bar">
        <div class="halves">
          {#if whole}<Meter class="whole" max={whole.total} value={whole.done} />{/if}
          <Meter class="part" value={part?.share ?? 0} />
        </div>
        <span class="meta">
          {#if whole}stages {whole.done}/{whole.total}{#if part}{" · "}{/if}{/if}{#if part}{run.stage} {part.text}{/if}
        </span>
      </div>
    {/if}

    {#if rows.length > 0}
      <h3>Stages</h3>
      <table class="stages">
        <thead><tr><th>stage</th><th>timeline</th><th class="n">time</th></tr></thead>
        <tbody>
          {#each rows as row (row.name)}
            <tr class={row.state}>
              <td>{row.name}</td>
              <td class="track-cell">
                <div class="track">
                  {#each row.spans as span, s (s)}
                    <span class:open={span.open} style:left={`${span.left}%`} style:width={`${span.width}%`}></span>
                  {/each}
                </div>
              </td>
              <td class="n">{row.seconds === null ? "" : duration(row.seconds)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}

    <h3>Events</h3>
    <ul class="events" bind:this={events}>
      {#each run.events as item, i (i)}
        {@const calls = item.seq == null ? 0 : (linked.get(item.seq) ?? 0)}
        <li>
          {#if run.started_at}<span class="meta">{offset(item.at ?? 0, 1)}</span>{/if}<span class="stage">{item.stage}</span>
          {item.detail}
          {#if calls > 0 && item.seq != null}
            {@const seq = item.seq}
            <button type="button" class="link" onclick={() => showCalls(seq)}>{calls} {calls === 1 ? "call" : "calls"}</button>
          {/if}
        </li>
      {/each}
    </ul>
  </Tabs.Content>

  <Tabs.Content value="steps">
    <Steps {run} oncalls={showCalls} />
  </Tabs.Content>

  <Tabs.Content value="usage">
    <div class="tiles">
      <div class="tile"><span class="label">elapsed</span><span class="value"><Clock from={run.started_at} to={run.ended_at} /></span></div>
      <div class="tile"><span class="label">tokens in</span><span class="value">{count(usage.prompt_tokens)}</span></div>
      <div class="tile">
        <span class="label">tokens out</span><span class="value">{count(usage.completion_tokens)}</span>
        {#if usage.reasoning_tokens}<span class="sub">{count(usage.reasoning_tokens)} reasoning</span>{/if}
      </div>
      <div class="tile"><span class="label">cost</span><span class="value">{usage.cost.toFixed(4)}</span></div>
      <div class="tile"><span class="label">searches</span><span class="value">{count(usage.searches)}</span></div>
      <div class="tile">
        <span class="label">embedding</span><span class="value">{count(usage.embedding_tokens)}</span><span class="sub">tokens</span>
      </div>
    </div>

    {#if Object.keys(usage.by_stage).length > 0}
      <h3>By stage</h3>
      <UsageTable title="stage" rows={usage.by_stage} />
    {/if}
    {#if Object.keys(usage.by_role).length > 0}
      <h3>By role</h3>
      <UsageTable title="role" rows={usage.by_role} />
    {/if}
    {#if Object.keys(usage.by_model).length > 0}
      <h3>By model</h3>
      <UsageTable title="model" rows={usage.by_model} />
    {/if}
  </Tabs.Content>

  <Tabs.Content value="calls">
    <Calls {run} bind:event />
  </Tabs.Content>

  <Tabs.Content value="searches">
    <Searches {run} bind:event />
  </Tabs.Content>

  {#if run.kind === "graph"}
    <Tabs.Content value="graph"><Graph {run} /></Tabs.Content>
  {/if}
</Tabs.Root>
