<script lang="ts">
  import { Controls, SvelteFlow, type Node } from "@xyflow/svelte";
  import "@xyflow/svelte/dist/style.css";
  import type { RunDetail } from "../lib/api";
  import { duration } from "../lib/format";
  import { layout, type StepData } from "../lib/layout";
  import { steps } from "../lib/steps";
  import StepNode from "./StepNode.svelte";
  import Viewport from "./Viewport.svelte";

  let { run, oncalls }: { run: RunDetail; oncalls: (seq: number) => void } = $props();

  const nodeTypes = { step: StepNode };

  const roots = $derived(steps(run));
  const current = $derived(run.ended_at === null ? roots.find((s) => s.state === "current")?.id : undefined);

  let open = $state(new Set<string>());
  // The current stage stays open as the run moves on, until the user opens or closes anything.
  let touched = false;
  $effect(() => {
    if (!touched) open = new Set(current ? [current] : []);
  });

  let selected = $state<string | null>(null);
  let focus = $state<{ ids: string[] } | null>(null);

  const laid = $derived(layout(roots, open));
  const nodes = $derived(laid.nodes.map((node) => ({ ...node, class: node.id === selected ? "selected" : undefined })));
  const chosen = $derived(laid.nodes.find((node) => node.id === selected)?.data.step ?? null);
  const took = $derived(chosen && chosen.started_at !== null && chosen.ended_at !== null ? chosen.ended_at - chosen.started_at : null);
  // Ids of an event step are `e:<seq>`; a page or a question has no event of its own.
  const seq = $derived(chosen?.id.startsWith("e:") ? Number(chosen.id.slice(2)) : null);

  function click({ node }: { node: Node }) {
    selected = node.id;
    const { step } = node.data as StepData;
    if (step.children.length === 0) return;
    touched = true;
    const next = new Set(open);
    if (!next.delete(node.id)) next.add(node.id);
    open = next;
    focus = { ids: [node.id, ...(next.has(node.id) ? step.children.map((child) => child.id) : [])] };
  }
</script>

<div class="steps-view">
  <SvelteFlow {nodes} edges={laid.edges} {nodeTypes} fitView minZoom={0.2} nodesDraggable={false} nodesConnectable={false} onnodeclick={click} proOptions={{ hideAttribution: true }}>
    <Controls showLock={false} />
    <Viewport {focus} />
  </SvelteFlow>
</div>

{#if chosen}
  <div class="step-chosen">
    <p><strong>{chosen.kind}</strong> · {chosen.label}</p>
    <p>{chosen.detail}</p>
    <p class="meta">
      {chosen.state}{#if took !== null} · {duration(took)}{/if}
      {#if chosen.started_at !== null} · started {new Date(chosen.started_at * 1000).toLocaleTimeString()}{/if}
      {#if chosen.ended_at !== null} · ended {new Date(chosen.ended_at * 1000).toLocaleTimeString()}{/if}
    </p>
    {#if chosen.calls.length > 0 && seq !== null}
      <button type="button" class="action" onclick={() => oncalls(seq)}>{chosen.calls.length} {chosen.calls.length === 1 ? "call" : "calls"}</button>
    {/if}
  </div>
{/if}
