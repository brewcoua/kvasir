<script lang="ts">
  import { Handle, Position, type NodeProps } from "@xyflow/svelte";
  import { count } from "../lib/format";
  import type { StepData } from "../lib/layout";
  import { icon, progressText } from "../lib/steps";
  import ClaimDots from "./ClaimDots.svelte";
  import Icon from "./Icon.svelte";
  import StepRow from "./StepRow.svelte";
  import StepStats from "./StepStats.svelte";

  let { data }: NodeProps & { data: StepData } = $props();

  const step = $derived(data.step);
</script>

<Handle type="target" position={Position.Left} isConnectable={false} class={{ linked: data.linkedIn }} />
<div class="step-group {step.state} {step.kind}" data-testid="step-{step.id}">
  <div class="step-head">
    {#if step.kind === "attempt" || step.kind === "recall"}
      <StepRow {step} />
    {:else}
      <Icon name={icon(step)} /><span class="step-label">{step.label}</span>
      {#if step.kind === "stage" && data.usage}<span class="step-stats"><StepStats usage={data.usage} /></span>{/if}
      {#if step.progress}<span class="meta" title={step.progress.detail}>{progressText(step.progress)}</span>{/if}
      {#if step.kind === "question"}
        {#if step.status}<span class="question-status {step.status}">{step.status}</span>{/if}
        <span class="meta">{count(step.count ?? 0)} {step.count === 1 ? "claim" : "claims"}</span>
      {:else}
        <span class="chip {step.state}">{step.state}</span>
      {/if}
    {/if}
    <span class="step-toggle">−</span>
  </div>
  {#if step.kind === "question" && step.claims.length > 0}
    <div class="claim-strip"><span class="meta">claims</span><ClaimDots {data} /></div>
  {/if}
</div>
<Handle type="source" position={Position.Right} isConnectable={false} class={{ linked: data.linkedOut }} />
