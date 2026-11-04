<script lang="ts">
  import { clock } from "../lib/clock.svelte";
  import { isCounter, meter, type Counter } from "../lib/steps";

  // `bare` leaves out the text, for a list too narrow for it.
  let { event, live = true, bare = false }: { event: Counter | null | undefined; live?: boolean; bare?: boolean } = $props();

  const shown = $derived(event && isCounter(event) ? meter(event, clock.now, live) : null);
</script>

{#if shown}
  <span class="bar"><progress max="1" value={shown.share} title={shown.text}></progress>{#if !bare}<span class="meta">{shown.text}</span>{/if}</span>
{/if}
