<script lang="ts">
  import type { Progress } from "../lib/api";

  let { event }: { event: Progress | null } = $props();
</script>

{#if event && event.total && event.done !== null && event.done !== undefined}
  <progress max={event.total} value={Math.min(event.done, event.total)} title={`${event.done}/${event.total}`}></progress>
{/if}
