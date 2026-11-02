import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { RunDetail } from "./api";
import { layout } from "./layout";
import { steps } from "./steps";

const graph: RunDetail = JSON.parse(readFileSync(join(import.meta.dirname, "..", "..", "fixtures", "graph.json"), "utf8"));

describe("layout", () => {
  const roots = steps(graph);
  const research = roots.find((s) => s.id === "stage:research")!;
  const count = (s: typeof research): number => s.children.reduce((n, c) => n + 1 + count(c), 0);

  it("shows the stages alone while nothing is open, chained in order", () => {
    const { nodes, edges } = layout(roots, new Set());
    expect(nodes.map((n) => n.id)).toEqual(roots.map((s) => s.id));
    expect(edges.map((e) => [e.source, e.target])).toEqual(roots.slice(1).map((s, i) => [roots[i].id, s.id]));
    expect(nodes.every((n) => n.type === "step" && !n.data.open)).toBe(true);
  });

  it("counts what a closed step hides", () => {
    const { nodes } = layout(roots, new Set());
    expect(nodes.find((n) => n.id === research.id)!.data.hidden).toBe(count(research));
  });

  it("adds the children of an open step, linked to it, left to right", () => {
    const { nodes, edges } = layout(roots, new Set([research.id]));
    const at = new Map(nodes.map((n) => [n.id, n]));
    expect(nodes).toHaveLength(roots.length + research.children.length);
    for (const child of research.children) {
      expect(edges.some((e) => e.source === research.id && e.target === child.id)).toBe(true);
      expect(at.get(child.id)!.position.x).toBeGreaterThan(at.get(research.id)!.position.x);
    }
    expect(at.get(research.id)!.data).toMatchObject({ open: true, hidden: 0 });
  });

  it("keeps ids stable and does not show children of a closed step", () => {
    const a = layout(roots, new Set([research.id]));
    const grandchild = research.children.find((c) => c.children.length > 0)!;
    const b = layout(roots, new Set([research.id, grandchild.id]));
    expect(b.nodes.map((n) => n.id)).toEqual(expect.arrayContaining(a.nodes.map((n) => n.id)));
    expect(b.nodes.length).toBe(a.nodes.length + grandchild.children.length);
    expect(new Set(b.nodes.map((n) => n.id)).size).toBe(b.nodes.length);
  });

  it("ignores an open id that is not visible", () => {
    const grandchild = research.children.find((c) => c.children.length > 0)!;
    expect(layout(roots, new Set([grandchild.id])).nodes).toHaveLength(roots.length);
  });
});
