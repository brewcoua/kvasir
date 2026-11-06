<script lang="ts">
  import { Progress } from "bits-ui";

  let { value, max = 1, class: className = "", title }: { value: number; max?: number; class?: string; title?: string } = $props();
</script>

<Progress.Root {value} {max} {title} class={["meter", className]}>
  <div class="meter-fill" style:width={`${max > 0 ? Math.min(100, (100 * value) / max) : 0}%`}></div>
</Progress.Root>
