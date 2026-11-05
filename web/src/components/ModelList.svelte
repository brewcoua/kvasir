<script lang="ts">
  import type { ModelInfo } from "../lib/api";
  import { describeModel } from "../lib/service";

  // A datalist rather than a select: a provider that cannot list its models can still be typed.
  let { id, models }: { id: string; models: ModelInfo[] } = $props();
</script>

<datalist {id}>
  {#each models as model (model.ref)}
    <option value={model.ref}>{describeModel(model)}</option>
  {/each}
</datalist>
