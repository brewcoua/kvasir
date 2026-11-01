<script lang="ts">
  import { isResearch, type ResearchQuestion, type RunDetail } from "../lib/api";
  import { isWeb } from "../lib/format";

  let { run }: { run: RunDetail } = $props();

  // Pixels per row and per level, shared by the rows and the lines drawn behind them.
  const ROW = 30;
  const INDENT = 22;
  const DOT = 8;

  const research = $derived(run.result && isResearch(run.result) ? run.result : null);
  // A run stored before questions were followed live still has them in its result.
  const questions = $derived(run.questions.length > 0 ? run.questions : (research?.questions ?? []));
  // Claims arrive only with the result, so a live graph shows how many each question rests on.
  const claims = $derived(new Map((research?.claims ?? []).map((claim) => [claim.id, claim])));

  // The questions form a forest, so each takes the next row in depth-first order and its level
  // as its indent. A child is always below its parent, which keeps every line short and straight.
  const rows = $derived.by(() => {
    const children = new Map<number | null, ResearchQuestion[]>();
    for (const q of questions) children.set(q.parent, [...(children.get(q.parent) ?? []), q]);
    const out: { question: ResearchQuestion; above: number | null }[] = [];
    const walk = (parent: number | null, above: number | null) => {
      for (const question of children.get(parent) ?? []) {
        const index = out.length;
        out.push({ question, above });
        walk(question.id, index);
      }
    };
    walk(null, null);
    return out;
  });

  const x = (depth: number) => depth * INDENT + DOT;
  const y = (row: number) => row * ROW + ROW / 2;
  const width = $derived(Math.max(0, ...rows.map((row) => x(row.question.depth))) + DOT);

  let selected: number | null = $state(null);
  const chosen = $derived(questions.find((q) => q.id === selected) ?? null);
</script>

{#if rows.length === 0}
  <p class="empty">No questions yet.</p>
{:else}
  <div class="graph" style:height={`${rows.length * ROW}px`}>
    <svg width={width} height={rows.length * ROW} aria-hidden="true">
      {#each rows as row, i (row.question.id)}
        {#if row.above !== null}
          <path d={`M ${x(row.question.depth - 1)} ${y(row.above)} V ${y(i)} H ${x(row.question.depth)}`} />
        {/if}
        <circle class={row.question.status} cx={x(row.question.depth)} cy={y(i)} r={DOT / 2 + 1} />
      {/each}
    </svg>
    {#each rows as row, i (row.question.id)}
      {@const q = row.question}
      <button
        type="button"
        class="node"
        class:selected={q.id === selected}
        style:top={`${i * ROW}px`}
        style:left={`${x(q.depth) + DOT + 4}px`}
        style:height={`${ROW}px`}
        onclick={() => (selected = selected === q.id ? null : q.id)}
      >
        <span class="text">{q.text}</span>
        <span class="meta">{q.status} · {q.queries.length} {q.queries.length === 1 ? "search" : "searches"} · {q.claims.length} {q.claims.length === 1 ? "claim" : "claims"}</span>
      </button>
    {/each}
  </div>

  {#if chosen}
    <div class="graph-chosen">
      <h3>{chosen.id}. {chosen.text}</h3>
      <p><span class="question-status {chosen.status}">{chosen.status}</span>{#if chosen.missing} · Missing: {chosen.missing}{/if}</p>
      {#if chosen.queries.length > 0}
        <h4>Queries</h4>
        <ul>{#each chosen.queries as query, i (i)}<li>{query}</li>{/each}</ul>
      {/if}
      {#if chosen.claims.length > 0}
        <h4>Claims</h4>
        {#if claims.size === 0}
          <p class="meta">Claims {chosen.claims.join(", ")}. Their text shows once the run is done.</p>
        {:else}
          <ul>
            {#each chosen.claims as id (id)}
              {@const claim = claims.get(id)}
              {#if claim}
                <li>
                  <span class="meta">[{claim.id}] {claim.confidence}</span> {claim.text}
                  <div class="meta">
                    {#if isWeb(claim.url)}<a href={claim.url} target="_blank" rel="noreferrer noopener">{claim.title || claim.url}</a>{:else}{claim.title || claim.url}{/if}
                    {#if claim.corroborated_by.length > 0} · corroborated by {claim.corroborated_by.join(", ")}{/if}
                    {#if claim.contradicted_by.length > 0} · contradicted by {claim.contradicted_by.join(", ")}{/if}
                  </div>
                </li>
              {/if}
            {/each}
          </ul>
        {/if}
      {/if}
    </div>
  {/if}
{/if}

