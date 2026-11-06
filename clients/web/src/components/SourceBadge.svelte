<script lang="ts">
  import type { SourceInfo } from "../lib/api";
  import { KIND_LABELS } from "../lib/format";

  // What kind of site it is and how far to trust it; nothing when the service does not classify sites.
  let { source, link = true }: { source: SourceInfo | null | undefined; link?: boolean } = $props();

  const text = $derived(source ? [KIND_LABELS[source.kind] ?? source.kind, ...(source.tier === "unknown" ? [] : [source.tier])].join(" · ") : "");
  const title = $derived(
    source ? [source.label, source.kind, `tier ${source.tier}`, source.tags.join(", "), source.origin && `from ${source.origin}`, source.listed_by?.length ? `listed by ${source.listed_by.join(", ")}` : "", source.evidence].filter(Boolean).join(" · ") : "",
  );
</script>

{#if source}
  {#if link && /^Q\d+$/.test(source.qid)}
    <a class={`source tier-${source.tier}`} {title} href={`https://www.wikidata.org/wiki/${source.qid}`} target="_blank" rel="noreferrer noopener">{text}</a>
  {:else}
    <span class={`source tier-${source.tier}`} {title}>{text}</span>
  {/if}
{/if}
