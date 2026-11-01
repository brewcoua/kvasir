<script lang="ts">
  import { Tooltip } from "bits-ui";
  import { isResearch, resultText, type RunResult } from "../lib/api";
  import { isWeb } from "../lib/format";
  import { provideNotes, type Note } from "../lib/notes";
  import Markdown from "./Markdown.svelte";
  import Ranking from "./Ranking.svelte";

  let { result }: { result: RunResult } = $props();

  const research = $derived(isResearch(result) ? result : null);
  const citations = $derived("citations" in result ? result.citations : []);
  const sources = $derived(new Map(citations.map((citation) => [citation.index, citation])));

  const tether = Tooltip.createTether<Note>();
  provideNotes({ tether, sources: () => sources });

  // The report's own table is the plain one, for clients that only read markdown.
  const body = $derived.by(() => {
    if (research?.output === "ranking" && research.matrix.length > 0) {
      return research.report.replace(/^## Ranking\n[\s\S]*?(?=\n## |$)/, "");
    }
    return resultText(result);
  });

  let copied = $state(false);
  async function copy(result: RunResult) {
    await navigator.clipboard.writeText(resultText(result));
    copied = true;
    setTimeout(() => (copied = false), 1500);
  }
</script>

<div class="result">
  <Tooltip.Provider delayDuration={0} disableHoverableContent>
    <div class="content">
      <div class="page">
        <p class="tools"><button type="button" class="action" onclick={() => copy(result)}>{copied ? "Copied" : "Copy"}</button></p>
        {#if "role" in result}<p class="meta">{result.role}</p>{/if}
        {#if research?.output === "ranking" && research.matrix.length > 0}<Ranking result={research} />{/if}
        <Markdown source={body} />

        {#if research && research.assumptions.length > 0}
          <details>
            <summary>Assumptions ({research.assumptions.length})</summary>
            <ul>
              {#each research.assumptions as assumption, i (i)}<li>{assumption}</li>{/each}
            </ul>
          </details>
        {/if}

        {#if research && research.questions.length > 0}
          <!-- Claim ids, like the removed sentences: they match the evidence table's # column. -->
          <details>
            <summary>Questions ({research.questions.length})</summary>
            <div class="scroll claims">
              <table>
                <thead><tr><th>#</th><th>question</th><th>status</th><th>queries</th><th>claims</th></tr></thead>
                <tbody>
                  {#each research.questions as question (question.id)}
                    <tr>
                      <td>{question.parent ? `${question.id} (after ${question.parent})` : question.id}</td>
                      <td>{question.missing ? `${question.text} Missing: ${question.missing}` : question.text}</td>
                      <td>{question.status}</td>
                      <td>{question.queries.join("; ")}</td>
                      <td>{question.claims.join(", ")}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </details>
        {/if}

        {#if "outline" in result && result.outline}
          <details>
            <summary>Outline</summary>
            <pre class="prose">{result.outline}</pre>
          </details>
        {/if}

        {#if citations.length > 0}
          <!-- Open, so a citation marker has somewhere to jump to. -->
          <details open>
            <summary>Sources ({citations.length})</summary>
            <ol class="citations">
              {#each citations as citation (citation.index)}
                <li value={citation.index} id={`cite-${citation.index}`}>
                  {@render link(citation.title || citation.url, citation.url)}
                  {#if citation.snippet}<div class="meta">{citation.snippet}</div>{/if}
                </li>
              {/each}
            </ol>
          </details>
        {/if}

        {#if research && research.claims.length > 0}
          <!-- Every claim the run gathered, so any cited sentence can be checked against its quote. -->
          <details>
            <summary>Evidence ({research.claims.length} claims)</summary>
            <div class="scroll claims">
              <table>
                <thead><tr><th>#</th><th>claim</th><th>quote</th><th>source</th><th>confidence</th></tr></thead>
                <tbody>
                  {#each research.claims as claim (claim.id)}
                    <tr>
                      <td>{claim.id}</td><td>{claim.text}</td><td>{claim.quote}</td>
                      <td>{@render link(claim.title || claim.url, claim.url)}</td><td>{claim.confidence}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </details>
        {/if}

        {#if research && research.removed.length > 0}
          <details>
            <summary>Removed by verification ({research.removed.length} sentences)</summary>
            <!-- The markers here are claim ids, as the writer wrote them, so they match the
                 evidence table's # column rather than the source numbers above. -->
            <p class="meta">[n] is the claim id in the evidence table.</p>
            <div class="scroll claims">
              <table>
                <thead><tr><th>section</th><th>sentence</th><th>reason</th></tr></thead>
                <tbody>
                  {#each research.removed as item, i (i)}
                    <tr><td>{item.section}</td><td>{item.sentence}</td><td>{item.reason}</td></tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </details>
        {/if}
      </div>
    </div>

    <Tooltip.Root {tether}>
      {#snippet children({ payload })}
        {#if payload}
          <Tooltip.Content class="popover" sideOffset={6} collisionPadding={8}>
            <strong>{payload.title}</strong>
            {#if payload.meta}<div class="meta">{payload.meta}</div>{/if}
            {#each payload.quotes ?? [] as quote, i (i)}
              <div class="quote">{quote.quote}</div>
              {#if quote.meta}<div class="meta">{quote.meta}</div>{/if}
            {/each}
          </Tooltip.Content>
        {/if}
      {/snippet}
    </Tooltip.Root>
  </Tooltip.Provider>
</div>

{#snippet link(label: string, url: string)}
  {#if isWeb(url)}<a href={url} target="_blank" rel="noreferrer noopener">{label}</a>{:else}{label}{/if}
{/snippet}
