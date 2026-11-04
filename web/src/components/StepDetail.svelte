<script module lang="ts">
  export interface Peer {
    id: number;
    kind: "corroborates" | "contradicts";
  }
</script>

<script lang="ts">
  import { Panel } from "@xyflow/svelte";
  import type { ResearchClaim } from "../lib/api";
  import { duration } from "../lib/format";
  import { icon, progressText, type Step } from "../lib/steps";
  import Icon from "./Icon.svelte";

  let {
    chosen,
    claim,
    claims,
    peers,
    questions,
    onclose,
    oncalls,
  }: {
    chosen: Step | null;
    // The claim picked from a question card's dots.
    claim: number | null;
    claims: Map<number, ResearchClaim>;
    peers: Map<number, Peer[]>;
    // Title of the question each claim belongs to.
    questions: Map<number, string>;
    onclose: () => void;
    oncalls: (seq: number) => void;
  } = $props();

  const took = $derived(chosen && chosen.started_at !== null && chosen.ended_at !== null ? chosen.ended_at - chosen.started_at : null);
  // Ids of an event step are `e:<seq>`; a page or a question has no event of its own.
  const seq = $derived(chosen?.id.startsWith("e:") ? Number(chosen.id.slice(2)) : null);
  const time = (at: number) => new Date(at * 1000).toLocaleTimeString();

  let panel = $state<HTMLElement>();
  $effect(() => {
    if (chosen && claim !== null) panel?.querySelector(`[data-claim="${claim}"]`)?.scrollIntoView({ block: "nearest" });
  });
</script>

{#if chosen}
  <Panel position="top-right">
    <div class="step-panel nowheel nopan nodrag {chosen.kind}" bind:this={panel}>
      <div class="step-head">
        <Icon name={icon(chosen)} /><span class="step-label">{chosen.label}</span>
        <button type="button" class="step-close" aria-label="Close" onclick={onclose}><Icon name="close" /></button>
      </div>
      <div class="step-panel-scroll">
        <div class="step-panel-body">
          <p>{chosen.detail}</p>
          {#if chosen.query}<p class="meta">query: {chosen.query}</p>{/if}
          <p class="meta">
            {chosen.state}{#if took !== null} · {duration(took)}{/if}
            {#if chosen.started_at !== null} · started {time(chosen.started_at)}{/if}
            {#if chosen.ended_at !== null} · ended {time(chosen.ended_at)}{/if}
          </p>
          {#if chosen.calls.length > 0 && seq !== null}
            <button type="button" class="action" onclick={() => oncalls(seq)}>{chosen.calls.length} {chosen.calls.length === 1 ? "call" : "calls"}</button>
          {/if}
          {#if chosen.progress}
            <p class="meta" data-testid="step-progress">
              {#if chosen.progress.total}<progress max={chosen.progress.total} value={Math.min(chosen.progress.done, chosen.progress.total)}></progress>{/if}
              {progressText(chosen.progress)} · {chosen.progress.detail}
            </p>
          {/if}
          {#if chosen.kind === "question"}
            <p>
              {#if chosen.status}<span class="question-status {chosen.status}">{chosen.status}</span>{/if}
              <span class="meta">{chosen.count} {chosen.count === 1 ? "claim" : "claims"}</span>
            </p>
          {/if}
        </div>
        {#if chosen.log.length > 0}
          <ol class="step-log" aria-label="Log">
            {#each chosen.log as line (line.seq)}
              <li><span class="meta">{line.at === null ? "" : time(line.at)}</span> {line.detail}</li>
            {/each}
          </ol>
        {/if}
        {#if chosen.kind === "question"}
          <ul class="claim-list">
            {#each chosen.claims as c (c.id)}
              <li class:picked={c.id === claim} data-claim={c.id}>
                <div>{claims.get(c.id)?.text ?? `Claim ${c.id}`}</div>
                <div class="meta">{c.confidence} confidence</div>
                {#each peers.get(c.id) ?? [] as peer (`${peer.kind}:${peer.id}`)}
                  <div class="rel {peer.kind}">{peer.kind === "corroborates" ? "corroborated by" : "contradicted by"}</div>
                  <div>{claims.get(peer.id)?.text ?? `Claim ${peer.id}`}</div>
                  <div class="meta">{questions.get(peer.id) ?? ""} · {claims.get(peer.id)?.confidence ?? ""} confidence</div>
                {/each}
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    </div>
  </Panel>
{/if}
