<script lang="ts">
  import { Handle, Position, type NodeProps } from "@xyflow/svelte";
  import { count } from "../lib/format";
  import type { StepData } from "../lib/layout";
  import { icon, isCounter, isShort, outcome, progressText } from "../lib/steps";
  import Bar from "./Bar.svelte";
  import ClaimDots from "./ClaimDots.svelte";
  import Icon from "./Icon.svelte";
  import StepRow from "./StepRow.svelte";
  import StepStats from "./StepStats.svelte";

  let { data }: NodeProps & { data: StepData } = $props();

  const step = $derived(data.step);
  const usage = $derived(data.usage);
  const plural = (n: number, word: string) => `${count(n)} ${word}${n === 1 ? "" : "s"}`;
</script>

<Handle type="target" position={Position.Left} isConnectable={false} />
{#if step.kind === "attempt" || step.kind === "recall" || step.kind === "page"}
  <div class="step-card step-row {step.state} {step.kind}" title={step.detail} data-testid="step-{step.id}">
    <div class="step-head">
      <StepRow {step} />
      {#if data.expandable}<span class="step-toggle">{data.open ? "−" : `+${data.hidden}`}</span>{/if}
    </div>
  </div>
{:else}
  <div class="step-card {step.state} {step.kind} {step.status ?? ''} {step.kind === 'verdict' ? outcome(step) : ''}" class:short={isShort(step)} title={step.detail} data-testid="step-{step.id}">
    <div class="step-head">
      <Icon name={icon(step)} /><span class="step-label">{step.label}</span>
      {#if isShort(step) && step.count !== null}<span class="meta step-count">{count(step.count)}</span>{/if}
      {#if step.state !== "done"}<span class="chip {step.state}">{step.state}</span>{/if}
      {#if data.expandable}<span class="step-toggle">{data.open ? "−" : `+${data.hidden}`}</span>{/if}
    </div>
    {#if !isShort(step) && (step.kind === "question" || usage || step.progress || data.latest)}
      <div class="step-body">
        {#if step.kind === "stage"}
          {#if usage}
            <div class="step-stats"><StepStats {usage} /></div>
          {/if}
          {#if step.progress}
            <div class="step-line" title={step.progress.detail}>
              {#if isCounter(step.progress)}<Bar event={step.progress} live={step.state === "current"} />{:else}<span class="meta">{progressText(step.progress)}</span>{/if}
            </div>
          {:else if data.latest}<Bar event={data.latest} />{/if}
        {:else if step.kind === "question"}
          <div class="step-line">
            {#if step.status}<span class="question-status {step.status}">{step.status}</span>{/if}
            <span class="meta">{plural(step.count ?? 0, "claim")}</span>
          </div>
          <ClaimDots {data} />
        {/if}
      </div>
    {/if}
  </div>
{/if}
<Handle type="source" position={Position.Right} isConnectable={false} />
