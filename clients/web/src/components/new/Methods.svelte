<script lang="ts">
  import { RadioGroup } from "bits-ui";
  import type { Launch } from "../../lib/launch.svelte";
  import { METHODS } from "../../lib/methods";

  let { launch }: { launch: Launch } = $props();
</script>

<RadioGroup.Root
  value={launch.method.kind}
  onValueChange={launch.choose}
  orientation="horizontal"
  class="methods"
  aria-label="Method"
>
  {#each METHODS as method (method.kind)}
    <RadioGroup.Item value={method.kind} class="method">{method.label}</RadioGroup.Item>
  {/each}
</RadioGroup.Root>
