<script lang="ts">
  import { elapsed } from "../lib/clock.svelte";
  import { duration } from "../lib/format";

  // Keeps counting while `to` is null.
  let { from, to }: { from: number | null; to: number | null } = $props();
  const seconds = $derived(elapsed(from, to));
</script>

<span class="clock">{seconds === null ? "" : duration(seconds)}</span>
