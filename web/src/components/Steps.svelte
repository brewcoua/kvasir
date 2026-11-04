<script lang="ts">
  import { Background, BackgroundVariant, Controls, Panel, SvelteFlow, type Edge, type Node, type Rect } from "@xyflow/svelte";
  import "@xyflow/svelte/dist/style.css";
  import { isResearch, type RunDetail } from "../lib/api";
  import { layout, type StepData } from "../lib/layout";
  import { links, steps, type Step, type StepKind } from "../lib/steps";
  import Icon, { type IconName } from "./Icon.svelte";
  import LinkEdge from "./LinkEdge.svelte";
  import StepDetail, { type Peer } from "./StepDetail.svelte";
  import StepGroup from "./StepGroup.svelte";
  import StepNode from "./StepNode.svelte";
  import Viewport from "./Viewport.svelte";

  let { run, oncalls }: { run: RunDetail; oncalls: (seq: number) => void } = $props();

  const legend: { kind: StepKind; label: string; icon: IconName; k: string }[] = [
    { kind: "stage", label: "stage", icon: "stage", k: "var(--k-stage)" },
    { kind: "question", label: "question", icon: "question", k: "var(--k-question)" },
    { kind: "attempt", label: "search", icon: "search", k: "var(--k-attempt)" },
    { kind: "recall", label: "recall", icon: "page", k: "var(--k-attempt)" },
    { kind: "repair", label: "repair", icon: "verify", k: "var(--k-section)" },
    { kind: "page", label: "page", icon: "page", k: "var(--k-page)" },
    { kind: "section", label: "section", icon: "write", k: "var(--k-section)" },
    { kind: "verdict", label: "verdict", icon: "verify", k: "var(--bad)" },
    { kind: "event", label: "event", icon: "event", k: "var(--k-event)" },
  ];

  const keys: { kind: string; label: string }[] = [
    { kind: "raised", label: "raised question" },
    { kind: "drew", label: "section drew on" },
    { kind: "removed", label: "removed from (width = count)" },
    { kind: "corroborates", label: "claims corroborate" },
    { kind: "contradicts", label: "claims contradict" },
  ];

  const nodeTypes = { step: StepNode, group: StepGroup };
  const edgeTypes = { trunk: LinkEdge, arc: LinkEdge, rail: LinkEdge };

  const roots = $derived(steps(run));
  const linked = $derived(links(run, roots));
  const everything = $derived.by(() => {
    const out: Step[] = [];
    const collect = (list: Step[]) => list.forEach((s) => (out.push(s), collect(s.children)));
    collect(roots);
    return out;
  });

  // Stages and questions with something inside start open; the user's changes sit on top, so a run still
  // being followed opens what appears.
  const defaults = $derived(new Set(everything.filter((s) => (s.kind === "stage" || s.kind === "question" || s.kind === "repair") && s.children.length > 0).map((s) => s.id)));
  let opened = $state(new Set<string>());
  let closed = $state(new Set<string>());
  const open = $derived(new Set([...defaults, ...opened].filter((id) => !closed.has(id))));

  let selected = $state<string | null>(null);
  let claim = $state<number | null>(null);
  let hovered = $state<string | null>(null);
  let focus = $state<{ box: Rect } | null>(null);

  const research = $derived(run.result && isResearch(run.result) ? run.result : null);
  const claims = $derived(new Map((research?.claims ?? []).map((c) => [c.id, c])));
  const questions = $derived(new Map(everything.flatMap((s) => (s.kind === "question" ? s.claims.map((c) => [c.id, s.label] as const) : []))));
  // Each claim link seen from both claims.
  const peers = $derived.by(() => {
    const out = new Map<number, Peer[]>();
    const add = (id: number, peer: Peer) => out.set(id, [...(out.get(id) ?? []).filter((p) => p.id !== peer.id || p.kind !== peer.kind), peer]);
    for (const c of research?.claims ?? []) {
      // Runs stored before claims were linked have neither list.
      for (const [kind, ids] of [["corroborates", c.corroborated_by ?? []], ["contradicts", c.contradicted_by ?? []]] as const) {
        for (const id of ids) (add(c.id, { id, kind }), add(id, { id: c.id, kind }));
      }
    }
    return out;
  });
  const rings = $derived(Object.fromEntries([...peers].map(([id, list]) => [id, list.some((p) => p.kind === "contradicts") ? "contradicts" : "corroborates"])));

  const laid = $derived(layout(roots, open, linked));
  const live = $derived(run.ended_at === null);
  // What the hovered card touches stays lit, and nothing else on a card; containers never dim.
  const near = $derived(new Set(laid.edges.flatMap((e) => (e.source === hovered || e.target === hovered ? [e.source, e.target] : []))));
  const nodes = $derived(
    laid.nodes.map((node) => {
      const { step } = node.data;
      const stage = step.kind === "stage" ? step.label : null;
      // Snapshots: xyflow warns when a node holds the run's reactive proxies.
      const base = stage === null ? node.data : { ...node.data, usage: $state.snapshot(run.usage.by_stage[stage]), latest: live && step.state === "current" && run.latest?.stage === stage ? $state.snapshot(run.latest) : undefined };
      const data = step.kind === "question" ? { ...base, rings, onclaim: pick } : base;
      const dim = hovered !== null && !node.data.container && !near.has(node.id) && node.id !== hovered;
      return { ...node, data, class: [node.id === selected ? "selected" : "", dim ? "dim" : ""].join(" ").trim() || undefined };
    }),
  );
  const edges = $derived(laid.edges.map((e): Edge => (hovered !== null && e.source !== hovered && e.target !== hovered ? { ...e, class: `${e.class ?? ""} dim`.trim(), data: e.data && { ...e.data, dim: true } } : e)));
  const chosen = $derived(laid.nodes.find((node) => node.id === selected)?.data.step ?? null);

  // Children are positioned inside their parent, so a node's place on the canvas adds up its ancestors'.
  function box(id: string): Rect {
    const at = new Map(laid.nodes.map((node) => [node.id, node]));
    const node = at.get(id)!;
    let x = node.position.x;
    let y = node.position.y;
    for (let parent = node.parentId; parent; parent = at.get(parent)!.parentId) {
      x += at.get(parent)!.position.x;
      y += at.get(parent)!.position.y;
    }
    return { x, y, width: node.width!, height: node.height! };
  }

  function pick(question: string, id: number) {
    selected = question;
    claim = id;
  }

  function click({ node, event }: { node: Node; event: MouseEvent | TouchEvent }) {
    selected = node.id;
    claim = null;
    const { expandable, container } = node.data as StepData;
    // A container collapses from its header only; its body is the cards inside it.
    if (!expandable || (container && !(event.target as Element).closest(".step-head"))) return;
    const opening = !open.has(node.id);
    opened = new Set(opening ? [...opened, node.id] : [...opened].filter((id) => id !== node.id));
    closed = new Set(opening ? [...closed].filter((id) => id !== node.id) : [...closed, node.id]);
    if (opening) focus = { box: box(node.id) };
  }
</script>

<div class="steps-view">
  <SvelteFlow
    {nodes}
    {edges}
    {nodeTypes}
    {edgeTypes}
    minZoom={0.2}
    nodesDraggable={false}
    nodesConnectable={false}
    onnodeclick={click}
    onnodepointerenter={({ node }) => (hovered = (node.data as StepData).container ? null : node.id)}
    onnodepointerleave={() => (hovered = null)}
    onpaneclick={() => (selected = null)}
    proOptions={{ hideAttribution: true }}
  >
    <Background variant={BackgroundVariant.Dots} gap={20} size={1.5} />
    <Controls showLock={false} />
    <Panel position="bottom-right">
      <ul class="step-legend" aria-label="Step kinds">
        {#each legend as item (item.kind)}
          <li data-kind={item.kind} style:--k={item.k}><Icon name={item.icon} />{item.label}</li>
        {/each}
        {#each keys as key (key.kind)}
          <li data-link={key.kind}>
            <svg viewBox="0 0 26 8" class="link-key {key.kind}" aria-hidden="true"><path d="M1 4H25" /></svg>{key.label}
          </li>
        {/each}
      </ul>
    </Panel>
    <Viewport {focus} first={laid.band} />
    <StepDetail {chosen} {claim} {claims} {peers} {questions} onclose={() => (selected = null)} {oncalls} />
  </SvelteFlow>
</div>
