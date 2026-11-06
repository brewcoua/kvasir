<script module lang="ts">
  import type { SourceInfo } from "../lib/api";

  export interface Site {
    domain: string;
    pages: number;
    links: number;
    source?: SourceInfo | null;
  }
  export interface SiteEdge {
    source: string;
    target: string;
    count: number;
  }

  // A site is labelled once it is this many pixels wide on screen: big ones first, more as you zoom in.
  const LABEL_PX = 12;
  export const labelVisible = (r: number, k: number) => r * k >= LABEL_PX;
</script>

<script lang="ts">
  import { drag } from "d3-drag";
  import { forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY, type SimulationLinkDatum, type SimulationNodeDatum } from "d3-force";
  import { select } from "d3-selection";
  import { zoom, type ZoomBehavior } from "d3-zoom";
  import { onDestroy, untrack } from "svelte";
  import { SvelteSet } from "svelte/reactivity";
  import SourceBadge from "./SourceBadge.svelte";

  let {
    sites,
    edges,
    size,
    selected,
    onpick,
  }: { sites: Site[]; edges: SiteEdge[]; size: "pages" | "links"; selected: string | null; onpick: (domain: string) => void } = $props();

  const LABEL = 24;
  const MIN_R = 6;
  const MAX_R = 34;
  // Icons load over the same few HTTP/1.1 connections as the API (about 6 per host), an uncached one can take
  // up to 10s, and an SVG <image> cannot load lazily, so only the biggest sites get one.
  const ICON_MIN_R = 10;
  const ICON_MAX = 60;

  interface Node extends SimulationNodeDatum {
    id: string;
    label: string;
    pages: number;
    links: number;
    source: SourceInfo | null;
    r: number;
    // 0..1, the same value that sets the radius; it also sets the shade.
    t: number;
  }
  interface Link extends SimulationLinkDatum<Node> {
    id: string;
    count: number;
  }

  const radiusOf = (t: number) => MIN_R + (MAX_R - MIN_R) * t;
  const shade = (value: number, biggest: number) => (biggest > 0 ? Math.sqrt(value / biggest) : 0);
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
        .distance(90)
        .strength(0.3),
    )
    .force("charge", forceManyBody<Node>().strength(-260))
    .force("collide", forceCollide<Node>((node) => node.r + 4))
    .force("x", forceX(0).strength(0.04))
    .force("y", forceY(0).strength(0.04))
    .on("tick", () => tick++);
  onDestroy(() => simulation.stop());

  // Rebuilt whenever the sites, edges or size change, keeping where every known site already is,
  // so the layout settles again from where it was rather than starting over.
  $effect(() => {
    const biggest = Math.max(0, ...sites.map((site) => site[size]));
    const next: Node[] = sites.map((site) => {
      const t = shade(site[size], biggest);
      return { id: site.domain, label: short(site.domain), pages: site.pages, links: site.links, source: site.source ?? null, r: radiusOf(t), t };
    });
    const known = new Set(next.map((node) => node.id));
    const nextLinks: Link[] = edges
      .filter((edge) => edge.source !== edge.target && known.has(edge.source) && known.has(edge.target))
      .map((edge) => ({ id: `${edge.source}>${edge.target}`, source: edge.source, target: edge.target, count: edge.count }));

    for (const node of next) {
      const old = byId.get(node.id);
      if (old) Object.assign(node, { x: old.x, y: old.y, vx: old.vx, vy: old.vy });
    }
    const grew = next.length !== nodes.length;
    byId.clear();
    for (const node of next) byId.set(node.id, node);
    nodes = next;
    links = nextLinks;
    simulation.nodes(nodes);
    simulation.force<ReturnType<typeof forceLink<Node, Link>>>("link")!.links(links);
    simulation.alpha(grew ? 0.6 : 0.2).restart();
    untrack(() => tick++);
  });

  // Depends on the sites and the size only, not on `tick`, so the set does not change while the layout moves.
  const iconIds = $derived.by(() => {
    const biggest = Math.max(0, ...sites.map((site) => site[size]));
    return new Set(
      sites
        .map((site) => ({ id: site.domain, r: radiusOf(shade(site[size], biggest)) }))
        .filter((site) => site.r >= ICON_MIN_R)
        .sort((a, b) => b.r - a.r)
        .slice(0, ICON_MAX)
        .map((site) => site.id),
    );
  });
  // Ids whose icon failed to load; the circle alone stays.
  const brokenIcons = new SvelteSet<string>();

  // Copies, so the template sees new positions on every tick.
  const shown = $derived.by(() => {
    void tick;
    return {
      nodes: nodes.map((node) => ({ ...node, x: node.x ?? 0, y: node.y ?? 0 })),
      links: links.map((link) => {
        const source = link.source as Node;
        const target = link.target as Node;
        return { id: link.id, width: 1 + Math.log2(Math.max(1, link.count)), x1: source.x ?? 0, y1: source.y ?? 0, x2: target.x ?? 0, y2: target.y ?? 0 };
      }),
    };
  });

  let width = $state(800);
  let height = $state(560);
  let transform = $state("");
  let zoomer: ZoomBehavior<SVGSVGElement, unknown> | null = null;
  // The view, plain: the card reads it when it opens, and zooming closes it.
  let view = { k: 1, x: 0, y: 0 };

  // Straight on the elements, so a zoom frame does not re-render the graph.
  function zoomable(element: SVGSVGElement) {
    const svg = select(element);
    zoomer = zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.2, 5])
      .on("zoom", (event) => {
        const { k, x, y } = event.transform;
        view = { k, x, y };
        transform = event.transform.toString();
        busy = true;
        svg.style("--inverse", 1 / k);
        svg.selectAll<SVGGElement, unknown>(".node").classed("labelled", function () {
          return labelVisible(Number(this.dataset.r), k);
        });
      })
      .on("end", () => (busy = false));
    select(element).call(zoomer);
  }

  function labelling(element: SVGGElement, r: number) {
    const apply = (r: number) => element.classList.toggle("labelled", labelVisible(r, view.k));
    apply(r);
    return { update: apply };
  }

  // The card follows the hovered site, but stays shut while a drag or a zoom is under way.
  let hovered = $state<string | null>(null);
  let busy = $state(false);
  let cardWidth = $state(0);
  let cardHeight = $state(0);

  const neighbours = $derived.by(() => {
    const known = new Set(sites.map((site) => site.domain));
    const found = new Map<string, Set<string>>();
    for (const { source, target } of edges) {
      if (source === target || !known.has(source) || !known.has(target)) continue;
      found.set(source, (found.get(source) ?? new Set()).add(target));
      found.set(target, (found.get(target) ?? new Set()).add(source));
    }
    return found;
  });

  const card = $derived.by(() => {
    void tick;
    const node = hovered && !busy ? byId.get(hovered) : undefined;
    if (!node) return null;
    const left = width / 2 + view.x + view.k * (node.x ?? 0) + node.r * view.k + 8;
    const top = height / 2 + view.y + view.k * (node.y ?? 0) - cardHeight / 2;
    return {
      node,
      connected: neighbours.get(node.id)?.size ?? 0,
      left: Math.max(0, Math.min(left, width - cardWidth)),
      top: Math.max(0, Math.min(top, height - cardHeight)),
    };
  });

  const leave = (id: string) => {
    if (hovered === id) hovered = null;
  };

  function draggable(element: SVGGElement, id: string) {
    const node = () => byId.get(id)!;
    select(element).call(
      drag<SVGGElement, unknown>()
        .subject(() => node())
        .on("start", (event) => {
          busy = true;
          if (!event.active) simulation.alphaTarget(0.3).restart();
          Object.assign(node(), { fx: node().x, fy: node().y });
        })
        .on("drag", (event) => Object.assign(node(), { fx: event.x, fy: event.y }))
        .on("end", (event) => {
          busy = false;
          if (!event.active) simulation.alphaTarget(0);
          Object.assign(node(), { fx: null, fy: null });
        }),
    );
  }
</script>

<div class="graph site-graph" bind:clientWidth={width} bind:clientHeight={height}>
  <svg
    use:zoomable
    width={width}
    height={height}
    viewBox={`${-width / 2} ${-height / 2} ${width} ${height}`}
    role="group"
    aria-label="Cached websites, and the links between them"
  >
    <g transform={transform}>
      {#each shown.links as link (link.id)}
        <line class="link" stroke-width={link.width} x1={link.x1} y1={link.y1} x2={link.x2} y2={link.y2} />
      {/each}
      {#each shown.nodes as node (node.id)}
        <g
          class={["node", node.source && `tier-${node.source.tier}`]}
          class:selected={node.id === selected}
          transform={`translate(${node.x},${node.y})`}
          style:--t={node.t}
          style:--r={`${node.r}px`}
          data-r={node.r}
          aria-label={`${node.id}, ${node.pages} pages, ${node.links} links`}
          use:draggable={node.id}
          use:labelling={node.r}
          onpointerenter={() => (hovered = node.id)}
          onpointerleave={() => leave(node.id)}
          onfocus={() => (hovered = node.id)}
          onblur={() => leave(node.id)}
          role="button"
          tabindex="0"
          onclick={() => onpick(node.id)}
          onkeydown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              onpick(node.id);
            }
          }}
        >
          {#if node.source}<title>{node.id}: {node.source.kind}, {node.source.tier}</title>{/if}
          <circle r={node.r} />
          {#if iconIds.has(node.id) && !brokenIcons.has(node.id)}
            <image
              class="favicon"
              href={`/v1/favicons/${encodeURIComponent(node.id)}`}
              x={-node.r}
              y={-node.r}
              width={2 * node.r}
              height={2 * node.r}
              onerror={() => brokenIcons.add(node.id)}
            />
            <circle class="ring" r={node.r} />
          {/if}
          <text dy="0.35em">{node.label}</text>
        </g>
      {/each}
    </g>
  </svg>
  {#if card}
    <div class="site-card" style:left={`${card.left}px`} style:top={`${card.top}px`} bind:clientWidth={cardWidth} bind:clientHeight={cardHeight}>
      <strong>{card.node.id}</strong>
      <span>{card.node.pages} pages</span>
      <span>{card.node.links} links</span>
      <span>{card.connected} connected sites</span>
      <SourceBadge source={card.node.source} />
    </div>
  {/if}
</div>
