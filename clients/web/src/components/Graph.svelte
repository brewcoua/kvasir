<script lang="ts">
  import { drag } from "d3-drag";
  import { forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY, type SimulationLinkDatum, type SimulationNodeDatum } from "d3-force";
  import { select } from "d3-selection";
  import { zoom, zoomIdentity, type ZoomBehavior } from "d3-zoom";
  import { onDestroy, untrack } from "svelte";
  import { isResearch, type ResearchClaim, type ResearchQuestion, type RunDetail } from "../lib/api";
  import { isWeb } from "../lib/format";

  let { run }: { run: RunDetail } = $props();

  const HEIGHT = 560;
  const LABEL = 36;

  type Kind = "topic" | "question" | "claim";
  interface Node extends SimulationNodeDatum {
    id: string;
    kind: Kind;
    label: string;
    // What hovering shows: the whole question or claim, where the label is short.
    full: string;
    r: number;
    status?: string;
    confidence?: string;
  }
  // `raised` joins a question to the one whose evidence raised it, or a first question to the topic.
  type LinkKind = "raised" | "rests" | "corroborates" | "contradicts";
  interface Link extends SimulationLinkDatum<Node> {
    id: string;
    kind: LinkKind;
  }

  const research = $derived(run.result && isResearch(run.result) ? run.result : null);
  // A run stored before questions were followed live still has them in its result.
  const questions = $derived(run.questions.length > 0 ? run.questions : (research?.questions ?? []));
  // Claims arrive only with the result, so a live graph has questions alone.
  const claims = $derived(new Map((research?.claims ?? []).map((claim) => [claim.id, claim])));

  const short = (text: string) => (text.length > LABEL ? `${text.slice(0, LABEL - 1)}…` : text);

  // Plain, not state: the simulation moves them, and `tick` tells the template when it has.
  let nodes: Node[] = [];
  let links: Link[] = [];
  const byId = new Map<string, Node>();
  let tick = $state(0);

  const simulation = forceSimulation<Node, Link>()
    .force(
      "link",
      forceLink<Node, Link>()
        .id((node) => node.id)
        .distance((link) => (link.kind === "rests" ? 28 : link.kind === "raised" ? 80 : 60))
        .strength((link) => (link.kind === "corroborates" || link.kind === "contradicts" ? 0.05 : 0.7)),
    )
    .force("charge", forceManyBody<Node>().strength((node) => (node.kind === "claim" ? -30 : -260)))
    .force("collide", forceCollide<Node>((node) => node.r + 3))
    .force("x", forceX(0).strength(0.04))
    .force("y", forceY(0).strength(0.04))
    .on("tick", () => tick++);
  onDestroy(() => simulation.stop());

  // Rebuilt whenever the run brings questions or claims, keeping where every known node already is,
  // so a live graph grows rather than starting over.
  $effect(() => {
    const next: Node[] = [{ id: "topic", kind: "topic", label: short(run.topic), full: run.topic, r: 11 }];
    const nextLinks: Link[] = [];
    const cited = new Set<number>();
    for (const q of questions) {
      next.push({ id: `q${q.id}`, kind: "question", label: q.title || short(q.text), full: q.text, r: q.depth === 0 ? 9 : 7, status: q.status });
      const above = q.parent === null ? "topic" : `q${q.parent}`;
      nextLinks.push({ id: `${above}-q${q.id}`, kind: "raised", source: above, target: `q${q.id}` });
      for (const id of q.claims) {
        if (!claims.has(id)) continue;
        cited.add(id);
        nextLinks.push({ id: `q${q.id}-c${id}`, kind: "rests", source: `q${q.id}`, target: `c${id}` });
      }
    }
    // Only the claims a question rests on: the rest would float free of the graph.
    for (const id of cited) {
      const claim = claims.get(id)!;
      next.push({ id: `c${id}`, kind: "claim", label: short(claim.text), full: claim.text, r: 4, confidence: claim.confidence });
      for (const [kind, others] of [
        ["corroborates", claim.corroborated_by],
        ["contradicts", claim.contradicted_by],
      ] as const) {
        for (const other of others) {
          if (cited.has(other) && other > id) nextLinks.push({ id: `c${id}-${kind}-c${other}`, kind, source: `c${id}`, target: `c${other}` });
        }
      }
    }

    for (const node of next) {
      const known = byId.get(node.id);
      if (known) Object.assign(node, { x: known.x, y: known.y, vx: known.vx, vy: known.vy });
    }
    // A new node starts beside the one it hangs from, so it does not fly in from the origin.
    const placed = new Map(next.map((node) => [node.id, node]));
    for (const link of nextLinks) {
      const target = placed.get(link.target as string)!;
      const source = placed.get(link.source as string);
      if (target.x === undefined && source?.x !== undefined) {
        target.x = source.x + (Math.random() - 0.5) * 30;
        target.y = (source.y ?? 0) + (Math.random() - 0.5) * 30;
      }
    }
    byId.clear();
    for (const node of next) byId.set(node.id, node);
    const grew = next.length !== nodes.length;
    nodes = next;
    links = nextLinks;
    simulation.nodes(nodes);
    simulation.force<ReturnType<typeof forceLink<Node, Link>>>("link")!.links(links);
    simulation.alpha(grew ? 0.6 : 0.2).restart();
    untrack(() => tick++);
  });

  // Copies, so the template sees new positions on every tick.
  const shown = $derived.by(() => {
    void tick;
    return {
      nodes: nodes.map((node) => ({ ...node, x: node.x ?? 0, y: node.y ?? 0 })),
      links: links.map((link) => {
        const source = link.source as Node;
        const target = link.target as Node;
        return { id: link.id, kind: link.kind, x1: source.x ?? 0, y1: source.y ?? 0, x2: target.x ?? 0, y2: target.y ?? 0, ends: [source.id, target.id] };
      }),
    };
  });

  let selected = $state<string | null>(null);
  // The selected node and those it links to, which stay bright while the rest fade.
  const near = $derived.by(() => {
    if (selected === null) return null;
    void tick;
    const ids = new Set([selected]);
    for (const link of shown.links) if (link.ends.includes(selected)) for (const end of link.ends) ids.add(end);
    return ids;
  });
  const chosenQuestion = $derived<ResearchQuestion | null>(
    selected?.startsWith("q") ? (questions.find((q) => `q${q.id}` === selected) ?? null) : null,
  );
  const chosenClaim = $derived<ResearchClaim | null>(selected?.startsWith("c") ? (claims.get(Number(selected.slice(1))) ?? null) : null);

  let width = $state(800);
  let transform = $state("");
  let zoomer: ZoomBehavior<SVGSVGElement, unknown> | null = null;
  let svg: SVGSVGElement | undefined = $state();

  function zoomable(element: SVGSVGElement) {
    zoomer = zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.2, 5])
      .on("zoom", (event) => (transform = event.transform.toString()));
    select(element).call(zoomer);
  }

  function draggable(element: SVGGElement, id: string) {
    const node = () => byId.get(id)!;
    select(element).call(
      drag<SVGGElement, unknown>()
        .subject(() => node())
        .on("start", (event) => {
          if (!event.active) simulation.alphaTarget(0.3).restart();
          Object.assign(node(), { fx: node().x, fy: node().y });
        })
        .on("drag", (event) => Object.assign(node(), { fx: event.x, fy: event.y }))
        .on("end", (event) => {
          if (!event.active) simulation.alphaTarget(0);
          Object.assign(node(), { fx: null, fy: null });
        }),
    );
  }

  const reset = () => svg && zoomer && select(svg).call(zoomer.transform, zoomIdentity);
  const toggle = (id: string) => (selected = selected === id ? null : id);
</script>

{#if questions.length === 0}
  <p class="empty">No questions yet.</p>
{:else}
  <p class="graph-legend meta">
    <span class="question-status answered">answered</span>
    <span class="question-status weak">weak</span>
    <span class="question-status contradicted">contradicted</span>
    <span>open or unresolved</span>
    <span>· small dots are claims{#if claims.size === 0}, shown once the run is done{/if}</span>
    <button type="button" class="action" onclick={reset}>Reset view</button>
  </p>
  <div class="graph" bind:clientWidth={width}>
    <!-- Clicking the background clears the selection; nodes are reached with the pointer only. -->
    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
    <svg
      bind:this={svg}
      use:zoomable
      width={width}
      height={HEIGHT}
      viewBox={`${-width / 2} ${-HEIGHT / 2} ${width} ${HEIGHT}`}
      role="img"
      aria-label="The questions of the run, and the claims they rest on"
      onclick={(event) => event.target === svg && (selected = null)}
    >
      <g transform={transform}>
        {#each shown.links as link (link.id)}
          <line class="link {link.kind}" class:faded={near !== null && !(near.has(link.ends[0]) && near.has(link.ends[1]))} x1={link.x1} y1={link.y1} x2={link.x2} y2={link.y2} />
        {/each}
        {#each shown.nodes as node (node.id)}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <g
            class="node {node.kind} {node.status ?? ''} {node.confidence ?? ''}"
            class:selected={node.id === selected}
            class:faded={near !== null && !near.has(node.id)}
            transform={`translate(${node.x},${node.y})`}
            use:draggable={node.id}
            role="button"
            tabindex="-1"
            onclick={() => toggle(node.id)}
          >
            <circle r={node.r} />
            {#if node.kind !== "claim"}<text x={node.r + 4} dy="0.35em">{node.label}</text>{/if}
            <title>{node.full}</title>
          </g>
        {/each}
      </g>
    </svg>
  </div>

  {#if chosenQuestion}
    {@const q = chosenQuestion}
    <div class="graph-chosen">
      <h3>{q.id}. {q.title || q.text}</h3>
      {#if q.title}<p>{q.text}</p>{/if}
      <p><span class="question-status {q.status}">{q.status}</span>{#if q.missing} · Missing: {q.missing}{/if}</p>
      {#if q.queries.length > 0}
        <h4>Queries</h4>
        <ul>{#each q.queries as query, i (i)}<li>{query}</li>{/each}</ul>
      {/if}
      {#if q.claims.length > 0}
        <h4>Claims</h4>
        {#if claims.size === 0}
          <p class="meta">Claims {q.claims.join(", ")}. Their text shows once the run is done.</p>
        {:else}
          <ul>
            {#each q.claims as id (id)}
              {@const claim = claims.get(id)}
              {#if claim}<li><button type="button" class="link" onclick={() => (selected = `c${id}`)}>[{id}]</button> {claim.text}</li>{/if}
            {/each}
          </ul>
        {/if}
      {/if}
    </div>
  {:else if chosenClaim}
    {@const claim = chosenClaim}
    <div class="graph-chosen">
      <h3>[{claim.id}] {claim.text}</h3>
      <p class="meta">{claim.confidence} confidence · {#if isWeb(claim.url)}<a href={claim.url} target="_blank" rel="noreferrer noopener">{claim.title || claim.url}</a>{:else}{claim.title || claim.url}{/if}</p>
      <blockquote>{claim.quote}</blockquote>
      {#if claim.corroborated_by.length > 0}<p>Corroborated by {claim.corroborated_by.join(", ")}</p>{/if}
      {#if claim.contradicted_by.length > 0}<p>Contradicted by {claim.contradicted_by.join(", ")}</p>{/if}
    </div>
  {/if}
{/if}
