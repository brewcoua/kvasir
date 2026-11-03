<script lang="ts">
  import { Handle, Position, type NodeProps } from "@xyflow/svelte";
  import { count, duration } from "../lib/format";
  import type { StepData } from "../lib/layout";
  import { icon, outcome, progressText } from "../lib/steps";
  import Bar from "./Bar.svelte";
  import ClaimDots from "./ClaimDots.svelte";
  import Icon from "./Icon.svelte";
  import StepStats from "./StepStats.svelte";

  let { data }: NodeProps & { data: StepData } = $props();

  const step = $derived(data.step);
  const took = $derived(step.started_at !== null && step.ended_at !== null ? step.ended_at - step.started_at : null);
  const usage = $derived(data.usage);
  const plural = (n: number, word: string) => `${count(n)} ${word}${n === 1 ? "" : "s"}`;
</script>

<Handle type="target" position={Position.Left} isConnectable={false} />
<div class="step-card {step.state} {step.kind} {step.status ?? ''} {step.kind === 'verdict' ? outcome(step) : ''}" title={step.detail} data-testid="step-{step.id}">
  <div class="step-head">
    <Icon name={icon(step)} /><span class="step-kind">{step.kind}</span>
    <span class="chip {step.state}">{step.state}</span>
    {#if data.expandable}<span class="step-toggle">{data.open ? "−" : `+${data.hidden}`}</span>{/if}
  </div>
  <div class="step-body">
    {#if step.kind === "stage"}
      <div class="step-title">{step.label}</div>
      {#if usage}
        <div class="step-stats"><StepStats {usage} /></div>
      {/if}
      {#if step.progress}
        <div class="step-line" title={step.progress.detail}>
          {#if step.progress.total}<progress max={step.progress.total} value={Math.min(step.progress.done, step.progress.total)}></progress>{/if}
          <span class="meta">{progressText(step.progress)}</span>
        </div>
      {:else if data.latest}<Bar event={data.latest} />{/if}
    {:else if step.kind === "question"}
      <div class="step-text">{step.label}</div>
      <div class="step-line">
        {#if step.status}<span class="question-status {step.status}">{step.status}</span>{/if}
        <span class="meta">{plural(step.count ?? 0, "claim")}</span>
      </div>
      <ClaimDots {data} />
    {:else if step.kind === "attempt"}
      <div class="step-text">{step.detail}</div>
      <div class="step-line step-stats">
        {#if step.count !== null}<span title="results"><Icon name="search" />{count(step.count)}</span>{/if}
        {#if step.added !== null}<span title="claims added">+{count(step.added)} {step.added === 1 ? "claim" : "claims"}</span>{/if}
        {#if took !== null}<span title="duration"><Icon name="clock" />{duration(took)}</span>{/if}
      </div>
    {:else if step.kind === "page"}
      <div class="step-text">{step.label}</div>
      <div class="step-line">
        {#each data.parts as part, i (i)}<span class="pip {part.state}"></span>{/each}
      </div>
    {:else}
      <div class="step-text">{step.label}</div>
      {#if step.count !== null}<div class="step-line"><span class="meta">{step.count}</span></div>{/if}
    {/if}
  </div>
</div>
<Handle type="source" position={Position.Right} isConnectable={false} />
