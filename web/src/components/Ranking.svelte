<script lang="ts">
  // Each cell is the option's place on that criterion, coloured from the best in its column to the
  // worst. Its finding and sources show on hover.
  import { Tooltip } from "bits-ui";
  import type { ResearchResult } from "../lib/api";
  import { host } from "../lib/format";
  import { useNotes, type Note } from "../lib/notes";

  let { result }: { result: ResearchResult } = $props();
  const { tether } = useNotes();

  const claims = $derived(new Map(result.claims.map((claim) => [claim.id, claim])));
  const numbers = $derived(new Map(result.citations.map((citation) => [citation.url, citation.index])));
  // The scale spans the cells shown, so the worst cited one is red even when empty ones rank below.
  const places = $derived(
    result.criteria.map((_, i) =>
      result.matrix.filter((row) => row.cells[i].claims.length > 0).map((row) => row.cells[i].rank ?? 0),
    ),
  );

  function colour(i: number, rank: number) {
    const best = Math.min(...places[i]);
    const worst = Math.max(...places[i]);
    const share = worst === best ? 1 : (worst - rank) / (worst - best);
    return `hsl(${Math.round(120 * share)} 70% 45% / 0.35)`;
  }

  // The finding, then each quote it rests on, with the source it was read from.
  function note(cell: ResearchResult["matrix"][number]["cells"][number]): Note {
    if (cell.claims.length === 0) {
      return { title: "No claim found", meta: `tied for last on this criterion, at ${cell.rank}` };
    }
    return {
      title: cell.finding,
      quotes: cell.claims
        .map((id) => claims.get(id))
        .filter((claim) => claim !== undefined)
        .map((claim) => ({ quote: claim.quote, meta: `[${numbers.get(claim.url) ?? "?"}] ${host(claim.url)}` })),
    };
  }
</script>

<div class="markdown">
  <h2>Ranking</h2>
  <div class="scroll">
    <table class="heat">
      <thead>
        <tr>
          <th>Rank</th><th>Option</th>
          {#each result.criteria as criterion (criterion.name)}
            <Tooltip.Trigger {tether} payload={{ title: criterion.name, meta: `weight ${criterion.weight} of 3 in the overall rank` }}>
              {#snippet child({ props })}<th {...props} class="n" tabindex="0">{criterion.name}</th>{/snippet}
            </Tooltip.Trigger>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each result.matrix as row (row.entity)}
          <tr>
            <td>{row.rank}</td><td>{row.entity}</td>
            {#each row.cells as cell, i (i)}
              <Tooltip.Trigger {tether} payload={note(cell)}>
                {#snippet child({ props })}
                  {#if cell.claims.length === 0}
                    <td {...props} class="n empty" tabindex="0">–</td>
                  {:else}
                    <td {...props} class="n" tabindex="0" style:background={colour(i, cell.rank ?? 0)}>{cell.rank}</td>
                  {/if}
                {/snippet}
              </Tooltip.Trigger>
            {/each}
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
  <p class="meta">
    Each cell is the option's place on that criterion, 1 being best, ranked twice with the options in
    reverse order the second time. Green is the best in its column, red the worst, a dash no claim
    found. Hover a cell for its finding and quotes, a criterion for its weight.
  </p>
</div>
