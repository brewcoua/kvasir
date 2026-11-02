<script lang="ts">
  import { Handle, Position, type NodeProps } from "@xyflow/svelte";
  import { duration } from "../lib/format";
  import type { StepData } from "../lib/layout";

  let { data }: NodeProps & { data: StepData } = $props();

  const step = $derived(data.step);
  const took = $derived(step.started_at !== null && step.ended_at !== null ? step.ended_at - step.started_at : null);
</script>

<Handle type="target" position={Position.Left} />
<div class="step-card {step.state} {step.kind} {step.status ?? ''}" class:stage={step.kind === "stage"} title={step.detail} data-testid="step-{step.id}">
  <div class="step-head">
    <span class="step-kind">{step.kind}</span>
    {#if step.status}<span class="question-status {step.status}">{step.status}</span>{/if}
    {#if step.count !== null}<span class="step-count">{step.count}</span>{/if}
    {#if step.calls.length > 0}<span class="meta">{step.calls.length} {step.calls.length === 1 ? "call" : "calls"}</span>{/if}
    {#if step.children.length > 0}<span class="step-toggle">{data.open ? "−" : `+${data.hidden}`}</span>{/if}
  </div>
  <div class="step-label">{step.label}</div>
  {#if took !== null}<div class="meta">{duration(took)}</div>{/if}
</div>
<Handle type="source" position={Position.Right} />
