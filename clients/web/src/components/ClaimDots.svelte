<script lang="ts">
  import type { StepData } from "../lib/layout";

  let { data }: { data: StepData } = $props();
</script>

<div class="claim-dots">
  {#each data.step.claims as claim (claim.id)}
    <button type="button" class="dot {claim.confidence} {data.rings?.[claim.id] ?? ''}" aria-label="claim {claim.id}" onclick={(event) => (event.stopPropagation(), data.onclaim?.(data.step.id, claim.id))}></button>
  {/each}
</div>
