<script lang="ts">
  import { Tooltip } from "bits-ui";
  import { host, isWeb } from "../lib/format";
  import { useNotes } from "../lib/notes";

  let { number }: { number: number } = $props();
  const notes = useNotes();
  const citation = $derived(notes?.sources().get(number));
</script>

<!-- Opens the source itself: an in-page `#cite-<n>` would be read as a route of the dashboard. -->
{#if citation && notes && isWeb(citation.url)}
  <Tooltip.Trigger
    tether={notes.tether}
    payload={{
      title: citation.title || host(citation.url),
      meta: citation.url,
      icon: citation.url,
      quotes: citation.snippet ? [{ quote: citation.snippet }] : [],
    }}
  >
    {#snippet child({ props })}
      <a {...props} class="cite" href={citation.url} target="_blank" rel="noreferrer noopener">
        {number}
      </a>
    {/snippet}
  </Tooltip.Trigger>
{:else}
  <span class="cite">{number}</span>
{/if}
