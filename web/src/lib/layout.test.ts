import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { RunDetail } from "./api";
import { HEADER, layout, PAD, PER_COLUMN } from "./layout";
import { links, steps, type Link, type Step } from "./steps";

const graph: RunDetail = JSON.parse(readFileSync(join(import.meta.dirname, "..", "..", "fixtures", "graph.json"), "utf8"));

const all = (list: Step[]): Step[] => list.flatMap((s) => [s, ...all(s.children)]);
const abs = (nodes: ReturnType<typeof layout>["nodes"]) => {
  const at = new Map(nodes.map((n) => [n.id, n]));
  const pos = (id: string): { x: number; y: number } => {
    const n = at.get(id)!;
    const p = n.parentId ? pos(n.parentId) : { x: 0, y: 0 };
    return { x: p.x + n.position.x, y: p.y + n.position.y };
  };
  return { at, pos };
};

describe("layout", () => {
  const roots = steps(graph);
  const linked = links(graph, roots);
  const research = roots.find((s) => s.id === "stage:research")!;
  const question = research.children.find((c) => c.kind === "question" && c.children.filter((k) => k.kind === "attempt").length > 1) ?? research.children.find((c) => c.kind === "question" && c.children.length > 0)!;
  const everything = new Set(all(roots).map((s) => s.id));
  const count = (s: Step): number => s.children.reduce((n, c) => n + 1 + count(c), 0);

  it("shows the stages alone while nothing is open, chained in order", () => {
    const { nodes, edges } = layout(roots, new Set(), linked);
    expect(nodes.map((n) => n.id)).toEqual(roots.map((s) => s.id));
    expect(edges.map((e) => [e.source, e.target])).toEqual(roots.slice(1).map((s, i) => [roots[i].id, s.id]));
    expect(nodes.every((n) => n.type === "step" && !n.data.container)).toBe(true);
    expect(nodes.every((n) => n.position.y === 0)).toBe(true);
  });

  it("counts what a closed step hides", () => {
    const { nodes } = layout(roots, new Set(), linked);
    expect(nodes.find((n) => n.id === research.id)!.data.hidden).toBe(count(research));
  });

  it("makes an open step a container holding its children", () => {
    const { nodes } = layout(roots, new Set([research.id]), linked);
    const at = new Map(nodes.map((n) => [n.id, n]));
    expect(nodes).toHaveLength(roots.length + research.children.length);
    expect(at.get(research.id)).toMatchObject({ type: "group", data: { open: true, hidden: 0, container: true } });
    for (const child of research.children) expect(at.get(child.id)).toMatchObject({ parentId: research.id, extent: "parent" });
  });

  it("puts parents before children, children inside the parent below its header, siblings apart", () => {
    const { nodes } = layout(roots, everything, linked);
    const index = new Map(nodes.map((n, i) => [n.id, i]));
    const box = (n: (typeof nodes)[number]) => ({ x: n.position.x, y: n.position.y, w: n.width!, h: n.height! });
    for (const n of nodes) {
      const siblings = nodes.filter((m) => m.parentId === n.parentId);
      for (const m of siblings) {
        if (m === n) continue;
        const a = box(n);
        const b = box(m);
        expect(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y, `${n.id} overlaps ${m.id}`).toBe(true);
      }
      if (!n.parentId) continue;
      expect(index.get(n.parentId)!).toBeLessThan(index.get(n.id)!);
      const parent = nodes.find((m) => m.id === n.parentId)!;
      const c = box(n);
      expect(c.x).toBeGreaterThanOrEqual(0);
      expect(c.y).toBeGreaterThanOrEqual(HEADER + PAD);
      expect(c.x + c.w).toBeLessThanOrEqual(parent.width!);
      expect(c.y + c.h).toBeLessThanOrEqual(parent.height!);
    }
  });

  it("starts the children of an open step a PAD below its header", () => {
    const { nodes } = layout(roots, new Set([research.id]), linked);
    expect(Math.min(...nodes.filter((n) => n.parentId === research.id).map((n) => n.position.y))).toBe(HEADER + PAD);
  });

  it("does not move a stage or anything before a step when it opens", () => {
    const before = layout(roots, new Set([research.id]), linked);
    const after = layout(roots, new Set([research.id, question.id]), linked);
    const a = abs(before.nodes);
    const b = abs(after.nodes);
    const cut = after.nodes.findIndex((n) => n.id === question.id);
    for (const n of after.nodes.slice(0, cut)) expect(b.pos(n.id), n.id).toEqual(a.pos(n.id));
    for (const root of roots.slice(0, roots.indexOf(research) + 1)) expect(b.pos(root.id)).toEqual(a.pos(root.id));
  });

  it("wraps a container's column every PER_COLUMN children", () => {
    const write = roots.find((s) => s.id === "stage:write")!;
    const column = write.children.filter((c) => c.kind !== "attempt" && c.kind !== "page").map((c) => c.id);
    expect(column.length).toBeGreaterThan(PER_COLUMN);
    const { at } = abs(layout(roots, new Set([write.id]), linked).nodes);
    const xs = [...new Set(column.map((id) => at.get(id)!.position.x))].sort((a, b) => a - b);
    expect(column.map((id) => xs.indexOf(at.get(id)!.position.x))).toEqual(column.map((_, i) => Math.floor(i / PER_COLUMN)));
  });

  it("keeps ids stable across calls", () => {
    const open = new Set([research.id, question.id]);
    expect(layout(roots, open, linked)).toEqual(layout(roots, open, linked));
    const ids = layout(roots, open, linked).nodes.map((n) => n.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("ignores an open id that is not visible", () => {
    expect(layout(roots, new Set([question.id]), linked).nodes).toHaveLength(roots.length);
  });

  it("never opens a page and lists its parts", () => {
    const pages = all(roots).filter((s) => s.kind === "page");
    expect(pages.length).toBeGreaterThan(0);
    const { nodes } = layout(roots, everything, linked);
    for (const page of pages) {
      const n = nodes.find((m) => m.id === page.id)!;
      expect(n).toMatchObject({ type: "step", data: { container: false, expandable: false } });
      expect(n.data.parts).toHaveLength(page.children.length);
    }
    expect(nodes.some((n) => n.parentId && pages.some((p) => p.id === n.parentId))).toBe(false);
  });

  it("chains the stages, and the attempts of an open question", () => {
    const { edges } = layout(roots, new Set([research.id, question.id]), linked);
    const chain = edges.map((e) => `${e.source}>${e.target}`);
    for (let i = 1; i < roots.length; i++) expect(chain).toContain(`${roots[i - 1].id}>${roots[i].id}`);
    const attempts = question.children.filter((c) => c.kind === "attempt");
    expect(attempts.length).toBeGreaterThan(1);
    for (let i = 1; i < attempts.length; i++) expect(chain).toContain(`${attempts[i - 1].id}>${attempts[i].id}`);
    // Containment is not an edge.
    expect(chain.some((l) => l.endsWith(`>${question.children[0].id}`) && l.startsWith(question.id))).toBe(false);
    expect(new Set(chain).size).toBe(chain.length);
  });

  it("animates only edges into the current step and classes the rest", () => {
    const live = steps({ ...graph, ended_at: null, state: "running" } as RunDetail);
    const state = new Map(all(live).map((s) => [s.id, s.state]));
    const { edges } = layout(live, new Set(all(live).map((s) => s.id)), []);
    expect(edges.some((e) => state.get(e.target) === "current")).toBe(true);
    for (const e of edges) {
      expect(!!e.animated).toBe(state.get(e.target) === "current");
      expect(e.class).toBe(["pending", "skipped"].includes(state.get(e.target)!) ? state.get(e.target) : "done");
    }
  });

  describe("research", () => {
    const open = new Set([research.id]);
    const questions = research.children.filter((c) => c.kind === "question");
    const split = research.children.find((c) => c.kind === "split")!;
    const nums = (path: string) => path.match(/-?[\d.]+/g)!.map(Number);
    const synthetic = (pairs: [number, number, Link["kind"]?][]): Link[] => [...linked, ...pairs.map(([a, b, kind]) => ({ from: `q:${a}`, to: `q:${b}`, kind: kind ?? "corroborates", count: 1 }))];
    // An arc's vertical x, and where it meets each card.
    const arcs = (edges: ReturnType<typeof layout>["edges"]) => edges.filter((e) => e.type === "arc").map((e) => {
      const n = nums((e.data as { path: string }).path);
      return { edge: e, xl: n[3] - 6, from: n[1], to: n[n.length - 2] };
    });

    it("lays questions out in columns by depth, left of them everything else", () => {
      const { nodes } = layout(roots, open, linked);
      const at = new Map(nodes.map((n) => [n.id, n]));
      const x = (s: Step) => at.get(s.id)!.position.x;
      const depths = [...new Set(questions.map((q) => q.depth))].sort((a, b) => a! - b!);
      expect(depths.length).toBeGreaterThan(1);
      for (const q of questions) expect(x(q)).toBe(x(questions.find((o) => o.depth === q.depth)!));
      const columns = depths.map((d) => x(questions.find((q) => q.depth === d)!));
      expect([...columns].sort((a, b) => a - b)).toEqual(columns);
      for (const s of research.children.filter((c) => c.kind !== "question")) {
        expect(x(s)).toBe(PAD);
        expect(x(s)).toBeLessThan(columns[0]);
      }
      const parentOf = new Map(linked.filter((l) => l.kind === "raised" && l.from.startsWith("q:")).map((l) => [l.to, l.from]));
      for (const q of questions.filter((q) => q.depth! > 0)) expect(at.get(q.id)!.position.y).toBeGreaterThanOrEqual(at.get(parentOf.get(q.id)!)!.position.y);
    });

    it("draws every raised link from the split step as a trunk edge", () => {
      const { edges } = layout(roots, open, linked);
      const raised = edges.filter((e) => e.source === split.id);
      expect(raised).toHaveLength(questions.filter((q) => q.depth === 0).length);
      expect(raised.every((e) => e.type === "trunk" && typeof (e.data as { path: string }).path === "string")).toBe(true);
      expect(edges.filter((e) => e.class === "raised" && !e.type)).toHaveLength(questions.filter((q) => q.depth! > 0).length);
    });

    it("draws no link whose end is not shown", () => {
      expect(layout(roots, new Set(), linked).edges.every((e) => e.type === undefined)).toBe(true);
    });

    it("gives each arc its own port on a card", () => {
      const { edges } = layout(roots, open, synthetic([[1, 2], [1, 3], [1, 4], [2, 3], [3, 5, "contradicts"], [4, 6]]));
      const ports = new Map<string, number[]>();
      for (const { edge, from, to } of arcs(edges)) {
        ports.set(edge.source, [...(ports.get(edge.source) ?? []), from]);
        ports.set(edge.target, [...(ports.get(edge.target) ?? []), to]);
      }
      expect(ports.get("q:1")).toHaveLength(3);
      for (const [id, ys] of ports) expect(new Set(ys).size, id).toBe(ys.length);
    });

    it("runs an arc outside the shorter arcs it overlaps, and a nested one inside", () => {
      const { edges } = layout(roots, open, synthetic([[1, 3], [2, 4], [1, 5], [3, 4]]));
      const level = new Map(arcs(edges).map((a) => [a.edge.id, a.xl]));
      const x = (a: number, b: number) => level.get(`corroborates:q:${a}>q:${b}`)!;
      expect(x(1, 3)).not.toBe(x(2, 4));
      // The vertical of the outer arc is further from the cards, so a smaller x.
      expect(x(1, 5)).toBeLessThan(x(1, 3));
      expect(x(1, 5)).toBeLessThan(x(2, 4));
    });

    it("folds links of questions raised from one to an arc between their roots, and drops one inside a root", () => {
      const { edges } = layout(roots, open, synthetic([[7, 3], [6, 3], [7, 8], [6, 7]]));
      const found = edges.filter((e) => e.type === "arc");
      expect(found.map((e) => [e.source, e.target, (e.data as { count: number }).count])).toEqual([["q:6", "q:3", 2]]);
    });

    it("keeps arc badges off the stubs of the trunk", () => {
      const { nodes, edges } = layout(roots, open, synthetic([[1, 2], [1, 3], [2, 4], [3, 5], [1, 6], [4, 5]]));
      const { pos } = abs(nodes);
      const stubs = edges.filter((e) => e.type === "trunk").map((e) => pos(e.target).y + HEADER / 2);
      const badges = edges.filter((e) => e.type === "arc").map((e) => (e.data as { badge: { y: number } }).badge.y);
      expect(badges).toHaveLength(6);
      for (const y of badges) for (const stub of stubs) expect(Math.abs(y - stub)).toBeGreaterThanOrEqual(12);
    });
  });
});
