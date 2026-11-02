<script lang="ts">
  import type { Token } from "../lib/markdown";
  import { isWeb } from "../lib/format";
  import Cite from "./Cite.svelte";
  import Inline from "./Inline.svelte";

  let { tokens }: { tokens: Token[] } = $props();
</script>

{#each tokens as token, index (index)}
  {#if token.type === "strong"}
    <strong><Inline tokens={token.tokens ?? []} /></strong>
  {:else if token.type === "em"}
    <em><Inline tokens={token.tokens ?? []} /></em>
  {:else if token.type === "del"}
    <del><Inline tokens={token.tokens ?? []} /></del>
  {:else if token.type === "codespan"}
    <code>{token.text}</code>
  {:else if token.type === "br"}
    <br />
  {:else if token.type === "link"}
    {#if isWeb(token.href)}
      <a href={token.href} target="_blank" rel="noreferrer noopener"><Inline tokens={token.tokens ?? []} /></a>
    {:else if token.href.startsWith("#")}
      <!-- A heading of the same text. Scrolled to rather than followed: the hash holds the route. -->
      <a
        href={token.href}
        onclick={(event) => {
          event.preventDefault();
          document.getElementById(decodeURIComponent(token.href.slice(1)))?.scrollIntoView({ behavior: "smooth" });
        }}><Inline tokens={token.tokens ?? []} /></a
      >
    {:else}
      <Inline tokens={token.tokens ?? []} />
    {/if}
  {:else if token.type === "image"}
    {token.text}
  {:else if token.type === "cite"}
    <Cite number={token.number} />
  {:else if token.type === "unverified"}
    <span class="unverified" title="No model could judge this sentence against its sources">unverified</span>
  {:else if token.type === "text" && token.tokens}
    <Inline tokens={token.tokens} />
  {:else if token.type === "text" || token.type === "escape"}
    {token.text}
  {:else}
    {token.raw}
  {/if}
{/each}
