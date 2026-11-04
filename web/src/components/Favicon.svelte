<script lang="ts">
  import { host, isWeb } from "../lib/format";

  // A page URL; the icon is the site's, served by the backend so the browser calls no third party.
  let { url }: { url: string } = $props();

  const src = $derived(`/v1/favicons/${encodeURIComponent(host(url))}`);
  // The source that failed, so a new url shows its own icon again.
  let failed = $state("");
</script>

{#if isWeb(url) && failed !== src}
  <img class="favicon" {src} alt="" width="16" height="16" loading="lazy" decoding="async" onerror={() => (failed = src)} />
{/if}
