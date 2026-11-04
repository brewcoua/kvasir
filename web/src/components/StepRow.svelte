<script lang="ts">
  import { count, duration } from "../lib/format";
  import { icon, type Step } from "../lib/steps";
  import Icon from "./Icon.svelte";

  let { step }: { step: Step } = $props();

  const took = $derived(step.started_at !== null && step.ended_at !== null ? step.ended_at - step.started_at : null);
  const plural = (n: number, word: string) => `${count(n)} ${word}${n === 1 ? "" : "s"}`;
</script>

<span class="row-fields">
  <Icon name={icon(step)} />
  {#if step.kind === "page"}
    <span class="row-text">{step.label}</span>
    {#each step.children as part, i (i)}<span class="pip {part.state}"></span>{/each}
  {:else}
    <span class="row-n">{step.kind === "attempt" ? `#${step.index}` : "recall"}</span>
    <span class="row-text">{step.query ?? ""}</span>
    <span class="step-stats">
      {#if step.count !== null}<span>{plural(step.count, step.kind === "attempt" ? "result" : "piece")}</span>{/if}
      {#if step.read !== null}<span>{plural(step.read, "page")}</span>{/if}
      {#if step.added !== null}<span>+{count(step.added)} {step.added === 1 ? "claim" : "claims"}</span>{/if}
      {#if took !== null}<span title="duration"><Icon name="clock" />{duration(took)}</span>{/if}
    </span>
  {/if}
</span>
