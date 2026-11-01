<script lang="ts">
  import type { ModelUsage } from "../lib/api";
  import { count, duration, rate } from "../lib/format";

  let { usage }: { usage: ModelUsage | undefined } = $props();
</script>

{#if usage}
  <td class="n">{count(usage.calls)}</td>
  <td class="n">{count(usage.prompt_tokens)}</td>
  <td class="n">{count(usage.completion_tokens)}</td>
  <td class="n">{count(usage.reasoning_tokens)}</td>
  <td class="n">{usage.cost.toFixed(4)}</td>
  <!-- Runs recorded before call times were kept have none. -->
  <td class="n">{usage.seconds ? duration(usage.seconds) : ""}</td>
  <td class="n">{rate(usage.completion_tokens, usage.seconds)}</td>
  <td class="n">{usage.seconds && usage.calls ? duration(usage.seconds / usage.calls) : ""}</td>
{:else}
  <td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td>
{/if}
