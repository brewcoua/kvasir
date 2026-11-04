import type { Edge, Node, Rect } from "@xyflow/svelte";
import type { ModelUsage, Progress } from "./api";
import type { Link, Step } from "./steps";

export const CARD = { width: 220, height: 100 };
export const STAGE = { width: 240, height: 116 };
// The header band of a card or container; handles sit at its centre (see .svelte-flow__handle in app.css).
export const HEADER = 36;
export const PAD = 12;
export const GAP = 16;
// Where a container's children start, so they do not touch its header.
const TOP = HEADER + PAD;
// Column children wrap into a new column after this many, so an open stage grows sideways into the free width.
// By count, not height: opening a card then only grows its own column, and no card changes column.
export const PER_COLUMN = 4;
// An open question or stage is at least this wide, so its title and stats fit in the header.
const QUESTION_WIDTH = 300;
const STAGE_WIDTH = 380;
// The strip of claim dots along the bottom of an open question; see .claim-strip in app.css.
const STRIP = 24;
// The first view of a run, at most.
const BAND = { width: 1200, height: 600 };
// Lane geometry for the arcs joining questions: each arc level takes ARC_STEP, and the first sits ARC_GAP off the cards.
const ARC_GAP = 6;
const ARC_STEP = 13;
// Corner radius of the trunk and arc paths.
const R = 6;

export interface StepData extends Record<string, unknown> {
  step: Step;
  open: boolean;
  // Descendants a closed step hides.
  hidden: number;
  // Drawn as a box around its children.
  container: boolean;
  expandable: boolean;
  // A page's parts, shown as markers on its card.
  parts: { state: Step["state"] }[];
  // Filled in by the caller, which has the run: a stage's spend, and the latest event while it is in that stage.
  usage?: ModelUsage;
  latest?: Progress;
  // A question's claims that other claims corroborate or contradict, and what clicking one does.
  rings?: Record<number, "corroborates" | "contradicts">;
  onclaim?: (question: string, claim: number) => void;
}

interface Box {
  step: Step;
  width: number;
  height: number;
  container: boolean;
  // Children in reading order; `row` ones run left to right, the rest stack below them in `columns`.
  row: Box[];
  columns: Box[][];
  // A container with questions puts its children by depth instead; positions are relative to it.
  research?: Research;
}

interface Arc {
  from: string;
  to: string;
  kind: "corroborates" | "contradicts";
  count: number;
  level: number;
  // Where it leaves `from` and enters `to`, and its badge, as y within the container.
  fromY: number;
  toY: number;
  badgeY: number;
}

interface Research {
  at: Map<string, { x: number; y: number }>;
  // Children in drawing order: those that are not questions, then the questions by depth.
  kids: Box[];
  trunk: number;
  // The x of the first question column, where the arcs end.
  lane: number;
  arcs: Arc[];
}

export interface LinkData extends Record<string, unknown> {
  // In flow coordinates; the edge component draws it as given.
  path: string;
  kind?: "corroborates" | "contradicts";
  count?: number;
  badge?: { x: number; y: number };
  // Set by the view while another card is hovered; the badge is not inside the edge element, so it cannot follow its class.
  dim?: boolean;
}

const descendants = (step: Step): number => step.children.reduce((n, child) => n + 1 + descendants(child), 0);
const expandable = (step: Step) => step.children.length > 0 && step.kind !== "page";
const inRow = (child: Step, parent: Step) => child.kind === "attempt" || child.kind === "page" || (child.kind === "event" && parent.kind === "attempt");

const sum = (sizes: number[]) => sizes.reduce((n, size) => n + size, 0) + GAP * Math.max(sizes.length - 1, 0);
const widest = (boxes: Box[]) => Math.max(...boxes.map((box) => box.width));

const isQuestion = (box: Box) => box.step.kind === "question";

// Questions go in columns by depth, left of them the other children, between the two a trunk for the links
// from the split step and a lane for the arcs between questions.
function research(step: Step, boxes: Box[], links: Link[]): Box {
  const parentOf = new Map(links.flatMap((l) => (l.kind === "raised" && l.from.startsWith("q:") ? [[l.to, l.from] as const] : [])));
  const loose = boxes.filter((box) => !isQuestion(box));
  const questions = boxes.filter(isQuestion);
  const depths = [...new Set(questions.map((box) => box.step.depth ?? 0))].sort((a, b) => a - b);
  const columns = depths.map((depth) => questions.filter((box) => (box.step.depth ?? 0) === depth));
  const at = new Map<string, { x: number; y: number }>();

  let y = TOP;
  for (const box of loose) {
    at.set(box.step.id, { x: PAD, y });
    y += box.height + GAP;
  }
  // A question sits level with its parent when that is free, else below the one above it; so deeper columns go in the order of their parents.
  const level = (box: Box) => at.get(parentOf.get(box.step.id) ?? "")?.y ?? 0;
  for (const [i, column] of columns.entries()) {
    y = TOP;
    if (i > 0) column.sort((a, b) => level(a) - level(b));
    for (const box of column) {
      const top = Math.max(y, level(box));
      at.set(box.step.id, { x: 0, y: top });
      y = top + box.height + GAP;
    }
  }

  const root = (id: string): string => (parentOf.has(id) ? root(parentOf.get(id)!) : id);
  const first = new Map((columns[0] ?? []).map((box) => [box.step.id, box]));
  const merged = new Map<string, Arc>();
  for (const l of links) {
    if (l.kind !== "corroborates" && l.kind !== "contradicts") continue;
    const [from, to] = [root(l.from), root(l.to)];
    if (from === to || !first.has(from) || !first.has(to)) continue;
    const key = `${l.kind}:${[from, to].sort().join(">")}`;
    const arc = merged.get(key) ?? { from, to, kind: l.kind, count: 0, level: 0, fromY: 0, toY: 0, badgeY: 0 };
    arc.count += l.count;
    merged.set(key, arc);
  }
  const arcs = [...merged.values()];
  const span = (arc: Arc) => [at.get(arc.from)!.y, at.get(arc.to)!.y].sort((a, b) => a - b);
  // Shortest first, and each arc runs outside every arc it overlaps, so nested ones never cross.
  const placed: Arc[] = [];
  for (const arc of [...arcs].sort((a, b) => span(a)[1] - span(a)[0] - (span(b)[1] - span(b)[0]))) {
    const [lo, hi] = span(arc);
    arc.level = 1 + Math.max(0, ...placed.filter((o) => span(o)[0] < hi && lo < span(o)[1]).map((o) => o.level));
    placed.push(arc);
  }

  const trunk = PAD + (loose.length > 0 ? widest(loose) : 0) + 10;
  const lane = trunk + ARC_GAP * 2 + 12 + ARC_STEP * Math.max(0, ...arcs.map((arc) => arc.level));
  let x = lane;
  for (const column of columns) {
    for (const box of column) at.get(box.step.id)!.x = x;
    x += widest(column) + GAP * 4;
  }

  // Ports on a card's left edge: arcs coming from above take the upper ones, shallow first, then those going down, deep first.
  const mine = (id: string) => arcs.filter((arc) => arc.from === id || arc.to === id);
  const above = (arc: Arc, id: string) => at.get(arc.from === id ? arc.to : arc.from)!.y < at.get(id)!.y;
  for (const [id, box] of first) {
    const list = [...mine(id).filter((arc) => above(arc, id)).sort((a, b) => a.level - b.level), ...mine(id).filter((arc) => !above(arc, id)).sort((a, b) => b.level - a.level)];
    const [top, bottom] = [at.get(id)!.y + 40, at.get(id)!.y + box.height - 14];
    list.forEach((arc, i) => (arc[arc.from === id ? "fromY" : "toY"] = list.length === 1 ? (top + bottom) / 2 : top + ((bottom - top) * i) / (list.length - 1)));
  }
  // Badges sit on the vertical, off the stubs of the trunk.
  const stubs = questions.filter((box) => !parentOf.has(box.step.id)).map((box) => at.get(box.step.id)!.y + HEADER / 2);
  for (const arc of arcs) {
    arc.badgeY = (arc.fromY + arc.toY) / 2;
    while (stubs.some((stub) => Math.abs(stub - arc.badgeY) < 12)) arc.badgeY += 8;
  }

  const kids = [...loose, ...columns.flat()];
  const bottom = Math.max(...kids.map((box) => at.get(box.step.id)!.y + box.height));
  return { step, width: x - GAP * 4 + PAD, height: bottom + PAD, container: true, row: [], columns: [], research: { at, kids, trunk, lane, arcs } };
}

function measure(step: Step, open: Set<string>, links: Link[]): Box {
  if (!(open.has(step.id) && expandable(step))) {
    const size = step.kind === "stage" ? STAGE : CARD;
    return { step, ...size, container: false, row: [], columns: [] };
  }
  const boxes = step.children.map((child) => measure(child, open, links));
  if (boxes.some(isQuestion)) return research(step, boxes, links);
  const row = boxes.filter((_, i) => inRow(step.children[i], step));
  const column = boxes.filter((_, i) => !inRow(step.children[i], step));
  const columns: Box[][] = [];
  for (let i = 0; i < column.length; i += PER_COLUMN) columns.push(column.slice(i, i + PER_COLUMN));
  const rowHeight = Math.max(0, ...row.map((box) => box.height));
  const rowWidth = row.length > 0 ? sum(row.map((box) => box.width)) : 0;
  const columnsWidth = columns.length > 0 ? sum(columns.map(widest)) : 0;
  const columnsHeight = Math.max(0, ...columns.map((boxes) => sum(boxes.map((box) => box.height))));
  const content = Math.max(step.kind === "question" ? QUESTION_WIDTH : step.kind === "stage" ? STAGE_WIDTH : CARD.width - 2 * PAD, rowWidth, columnsWidth);
  const strip = step.kind === "question" && step.claims.length > 0 ? STRIP + GAP : 0;
  const stacked = rowHeight + (row.length > 0 && columns.length > 0 ? GAP : 0) + columnsHeight + strip;
  return { step, width: content + 2 * PAD, height: TOP + stacked + PAD, container: true, row, columns };
}

// Roots always, then the children of every step in `open`, nested inside it. Opening a step moves only what follows it.
export function layout(roots: Step[], open: Set<string>, links: Link[]): { nodes: Node<StepData>[]; edges: Edge[]; band: Rect } {
  const nodes: Node<StepData>[] = [];
  const edges: Edge[] = [];
  const chain = (from: Step, to: Step) =>
    edges.push({ id: `${from.id}>${to.id}`, source: from.id, target: to.id, animated: to.state === "current", class: to.state === "pending" || to.state === "skipped" ? to.state : "done" });
  // Per research container, where its arcs and trunk are in flow coordinates.
  const lanes: { id: string; x: number; y: number; research: Research }[] = [];

  const place = (box: Box, x: number, y: number, parent?: string, origin = { x: 0, y: 0 }) => {
    const { step } = box;
    const isOpen = open.has(step.id);
    nodes.push({
      id: step.id,
      type: box.container ? "group" : "step",
      position: { x, y },
      width: box.width,
      height: box.height,
      ...(parent ? { parentId: parent, extent: "parent" as const } : {}),
      data: {
        step,
        open: isOpen,
        hidden: isOpen ? 0 : descendants(step),
        container: box.container,
        expandable: expandable(step),
        parts: step.kind === "page" ? step.children.map((part) => ({ state: part.state })) : [],
      },
    });
    const here = { x: origin.x + x, y: origin.y + y };
    if (box.research) {
      lanes.push({ id: step.id, ...here, research: box.research });
      for (const child of box.research.kids) {
        const { x: cx, y: cy } = box.research.at.get(child.step.id)!;
        place(child, cx, cy, step.id, here);
      }
      return;
    }
    let cx = PAD;
    box.row.forEach((child, i) => {
      place(child, cx, TOP, step.id, here);
      if (i > 0) chain(box.row[i - 1].step, child.step);
      cx += child.width + GAP;
    });
    const top = TOP + Math.max(0, ...box.row.map((child) => child.height)) + (box.row.length > 0 ? GAP : 0);
    cx = PAD;
    for (const column of box.columns) {
      let cy = top;
      for (const child of column) {
        place(child, cx, cy, step.id, here);
        cy += child.height + GAP;
      }
      cx += widest(column) + GAP;
    }
  };

  let x = 0;
  roots.forEach((root, i) => {
    const box = measure(root, open, links);
    place(box, x, 0);
    if (i > 0) chain(roots[i - 1], root);
    x += box.width + GAP * 3;
  });

  const at = new Map(nodes.map((node) => [node.id, node]));
  const flow = (id: string): { x: number; y: number } => {
    const node = at.get(id)!;
    const parent = node.parentId ? flow(node.parentId) : { x: 0, y: 0 };
    return { x: parent.x + node.position.x, y: parent.y + node.position.y };
  };
  for (const l of links) {
    const [from, to] = [at.get(l.from), at.get(l.to)];
    if (!from || !to || l.kind === "corroborates" || l.kind === "contradicts") continue;
    const id = `${l.kind}:${l.from}>${l.to}`;
    if (l.kind === "raised" && from.data.step.kind === "split") {
      const [a, b] = [flow(l.from), flow(l.to)];
      const trunk = lanes.find((lane) => lane.id === to.parentId)!;
      const [ax, ay, bx, by, tx] = [a.x + from.width!, a.y + HEADER / 2, b.x, b.y + HEADER / 2, trunk.x + trunk.research.trunk];
      const s = Math.sign(by - ay);
      const path = s === 0 ? `M${ax} ${ay}H${bx}` : `M${ax} ${ay}H${tx - R}Q${tx} ${ay} ${tx} ${ay + s * R}V${by - s * R}Q${tx} ${by} ${tx + R} ${by}H${bx}`;
      edges.push({ id, source: l.from, target: l.to, type: "trunk", class: "raised", data: { path } satisfies LinkData });
    } else if (l.kind === "removed") {
      edges.push({ id, source: l.from, target: l.to, class: "removed", style: `stroke-width: ${1 + 0.6 * l.count}px`, label: String(l.count) });
    } else {
      edges.push({ id, source: l.from, target: l.to, class: l.kind });
    }
  }
  for (const { x: ox, y: oy, research } of lanes) {
    for (const arc of research.arcs) {
      const xl = ox + research.lane - (ARC_GAP + arc.level * ARC_STEP);
      const [a, b] = [oy + arc.fromY, oy + arc.toY];
      const s = Math.sign(b - a);
      const x0 = ox + research.lane;
      const path = `M${x0} ${a}H${xl + R}Q${xl} ${a} ${xl} ${a + s * R}V${b - s * R}Q${xl} ${b} ${xl + R} ${b}H${x0}`;
      const data: LinkData = { path, kind: arc.kind, count: arc.count, badge: { x: xl, y: oy + arc.badgeY } };
      edges.push({ id: `${arc.kind}:${arc.from}>${arc.to}`, source: arc.from, target: arc.to, type: "arc", class: arc.kind, data });
    }
  }

  // The top left of the run, for the first view: the whole width would shrink the text of a wide research stage.
  const band: Rect = { x: 0, y: 0, width: Math.min(Math.max(x - GAP * 3, 0), BAND.width), height: BAND.height };
  return { nodes, edges, band };
}
