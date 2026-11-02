<script lang="ts">
  import { Tooltip } from "bits-ui";
  import { host } from "../lib/format";
  import { useNotes } from "../lib/notes";

  let { number }: { number: number } = $props();
  const notes = useNotes();
  const citation = $derived(notes?.sources().get(number));
</script>

{#if citation && notes}
  <Tooltip.Trigger
    tether={notes.tether}
    payload={{
      title: citation.title || host(citation.url),
      meta: citation.url,
      quotes: citation.snippet ? [{ quote: citation.snippet }] : [],
    }}
  >
    {#snippet child({ props })}
      <a {...props} class="cite" href={`#cite-${number}`}>
        {number}
      </a>
    {/snippet}
  </Tooltip.Trigger>
{:else}
  <a class="cite" href={`#cite-${number}`}>{number}</a>
{/if}
