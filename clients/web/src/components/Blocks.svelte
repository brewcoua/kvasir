<script lang="ts">
  import type { Token } from "../lib/markdown";
  import Blocks from "./Blocks.svelte";
  import Inline from "./Inline.svelte";

  let { tokens }: { tokens: Token[] } = $props();
</script>

{#each tokens as token, index (index)}
  {#if token.type === "space" || token.type === "def"}
    <!-- nothing to show -->
  {:else if token.type === "paragraph"}
    <p><Inline tokens={token.tokens ?? []} /></p>
  {:else if token.type === "heading"}
    <!-- The report's own `##` sits under the page title, so every level moves down one. -->
    <svelte:element this={`h${Math.min(token.depth + 1, 4)}`} id={token.id}><Inline tokens={token.tokens ?? []} /></svelte:element>
  {:else if token.type === "callout"}
    <aside class={`callout ${token.kind}`}>
      <div class="title">{token.title}</div>
      <Blocks tokens={token.tokens ?? []} />
    </aside>
  {:else if token.type === "boldLine"}
    <h4><Inline tokens={token.tokens ?? []} /></h4>
  {:else if token.type === "list"}
    <svelte:element
      this={token.ordered ? "ol" : "ul"}
      start={token.ordered && token.start !== 1 ? token.start : undefined}
    >
      {#each token.items as item, i (i)}
        <li><Blocks tokens={item.tokens} /></li>
      {/each}
    </svelte:element>
  {:else if token.type === "table"}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            {#each token.header as cell, i (i)}
              <th style:text-align={cell.align}><Inline tokens={cell.tokens} /></th>
            {/each}
          </tr>
        </thead>
        <tbody>
          {#each token.rows as row, r (r)}
            <tr>
              {#each row as cell, i (i)}
                <td style:text-align={cell.align}><Inline tokens={cell.tokens} /></td>
              {/each}
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if token.type === "blockquote"}
    <blockquote><Blocks tokens={token.tokens ?? []} /></blockquote>
  {:else if token.type === "code"}
    <pre><code>{token.text}</code></pre>
  {:else if token.type === "hr"}
    <hr />
  {:else if token.type === "text" && token.tokens}
    <!-- The text of a tight list item. -->
    <Inline tokens={token.tokens} />
  {:else}
    <p>{token.raw}</p>
  {/if}
{/each}
