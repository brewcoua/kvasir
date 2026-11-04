<script lang="ts">
  import { Handle, Position, type NodeProps } from "@xyflow/svelte";
  import { count } from "../lib/format";
  import type { StepData } from "../lib/layout";
  import { icon, outcome, progressText } from "../lib/steps";
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
      {:else}
        <div class="step-text">{step.label}</div>
        {#if step.count !== null}<div class="step-line"><span class="meta">{step.count}</span></div>{/if}
      {/if}
    </div>
  </div>
{/if}
<Handle type="source" position={Position.Right} isConnectable={false} />
