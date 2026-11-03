import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { isResearch, type Progress, type RunDetail } from "./api";
import { links, steps, type Step } from "./steps";

const graph: RunDetail = JSON.parse(readFileSync(join(import.meta.dirname, "..", "..", "fixtures", "graph.json"), "utf8"));

const walk = (list: Step[]): Step[] => list.flatMap((s) => [s, ...walk(s.children)]);
const find = (list: Step[], id: string) => walk(list).find((s) => s.id === id)!;

describe("steps", () => {
  const roots = steps(graph);
  const stage = (name: string) => roots.find((s) => s.id === `stage:${name}`)!;

  it("has a root per stage, in order", () => {
    expect(roots.map((s) => s.label)).toEqual(["brief", "research", "write", "verify"]);
  });

  it("holds every question under research, ordered by depth then id", () => {
    const qs = stage("research").children.filter((s) => s.kind === "question");
    expect(qs.map((s) => s.id).sort()).toEqual(graph.questions.map((q) => `q:${q.id}`).sort());
    expect(qs.some((s) => s.depth! > 0)).toBe(true);
    expect(qs.map((s) => [s.depth, s.question])).toEqual(graph.questions.map((q) => [q.depth, q.id]).sort((a, b) => a[0] - b[0] || a[1] - b[1]));
    expect(walk(qs).filter((s) => s.kind === "question")).toHaveLength(qs.length);
    expect(qs.every((s) => s.children.every((c) => c.kind !== "question"))).toBe(true);
  });

  it("makes the split event a split step", () => {
    const split = stage("research").children.filter((s) => s.kind === "split");
    expect(split.map((s) => s.detail)).toEqual(["splitting the brief into questions"]);
  });

  it("lists a question's claims with their confidence", () => {
    const q = graph.questions.find((q) => q.claims.length > 0)!;
    const claims = find(roots, `q:${q.id}`).claims;
    expect(claims.map((c) => c.id)).toEqual(q.claims);
    expect(claims.every((c) => ["high", "medium", "low"].includes(c.confidence))).toBe(true);
  });

  it("orders a column by start time, steps with no time last", () => {
    const sorted = (list: Step[]) => list.map((s) => s.started_at ?? Infinity).every((t, i, all) => i === 0 || all[i - 1] <= t);
    for (const s of walk(roots)) expect(sorted(s.children.filter((c) => c.kind !== "question"))).toBe(true);
    const first = stage("research").children[0];
    expect(first.kind).not.toBe("question");
  });

  it("splits an attempt into pages and parts", () => {
    const attempt = find(roots, "q:1").children.find((s) => s.kind === "attempt")!;
    expect(attempt.search).toBe(0);
    const parts = graph.events.filter((e) => e.kind === "part" && e.parent === Number(attempt.id.slice(2)));
    const pages = attempt.children.filter((s) => s.kind === "page");
    expect(pages.flatMap((p) => p.children)).toHaveLength(parts.length);
    expect(pages.map((p) => p.id)).toEqual([1, 2, 3].map((n) => `p:${attempt.id.slice(2)}:${n}`));
    expect(attempt.children.at(-1)!.kind).toBe("event");
  });

  it("puts every call on exactly one step", () => {
    const ids = walk(roots).flatMap((s) => s.calls);
    const sorted = (list: (number | null | undefined)[]) => list.map(Number).sort((a, b) => a - b);
    expect(sorted(ids)).toEqual(sorted(graph.calls.map((c) => c.id)));
  });

  it("folds verify progress counters into one step", () => {
    const events = stage("verify").children.filter((s) => s.kind === "event");
    expect(events).toHaveLength(1);
    expect(events[0].detail).toBe(graph.events.at(-1)!.detail);
  });

  it("makes a section step per section", () => {
    expect(stage("write").children.filter((s) => s.kind === "section")).toHaveLength(6);
  });

  it("counts the sentences removed by each tier", () => {
    const removed = isResearch(graph.result!) ? graph.result.removed : [];
    const verdicts = stage("verify").children.filter((s) => s.kind === "verdict");
    expect(verdicts.reduce((n, v) => n + v.count!, 0)).toBe(removed.length);
    expect(verdicts.map((v) => v.id)).toEqual([...new Set(removed.map((r) => `v:${r.stage}`))]);
  });

  it("carries a question's status and no other step's", () => {
    for (const s of walk(roots)) {
      const q = graph.questions.find((q) => `q:${q.id}` === s.id);
      expect(s.status).toBe(s.kind === "question" ? q!.status : null);
    }
    expect(new Set(walk(roots).flatMap((s) => (s.status ? [s.status] : []))).size).toBeGreaterThan(1);
  });

  it("keeps step ids unique", () => {
    const ids = walk(roots).map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("links", () => {
  const roots = steps(graph);
  const result = graph.result as Extract<typeof graph.result, { claims: unknown }> & { removed: { section: string }[] };
  const of = (kind: string) => links(graph, roots).filter((l) => l.kind === kind);

  it("raises each question from the split step or its parent", () => {
    const raised = of("raised");
    expect(raised).toHaveLength(graph.questions.length);
    const split = find(roots, "stage:research").children.find((s) => s.kind === "split")!;
    const rootIds = graph.questions.filter((q) => q.parent === null).map((q) => `q:${q.id}`);
    expect(raised.filter((l) => l.from === split.id).map((l) => l.to).sort()).toEqual(rootIds.sort());
    for (const q of graph.questions.filter((q) => q.parent !== null)) expect(raised).toContainEqual({ from: `q:${q.parent}`, to: `q:${q.id}`, kind: "raised", count: 1 });
  });

  it("draws one section per section step and counts removals", () => {
    expect(of("drew")).toHaveLength(walk(roots).filter((s) => s.kind === "section").length);
    const texts = new Set(graph.questions.map((q) => q.text));
    expect(of("removed").reduce((n, l) => n + l.count, 0)).toBe(result.removed.filter((r) => texts.has(r.section)).length);
  });

  it("references only steps in the tree", () => {
    const ids = new Set(walk(roots).map((s) => s.id));
    for (const l of links(graph, roots)) expect([ids.has(l.from), ids.has(l.to)]).toEqual([true, true]);
  });

  it("links questions by corroborating and contradicting claims, once per claim pair", () => {
    const run: RunDetail = structuredClone(graph);
    const [q1, q2] = run.questions.filter((q) => q.claims.length >= 2);
    const claims = (run.result as typeof result).claims as { id: number; corroborated_by: number[]; contradicted_by: number[] }[];
    const c = (id: number) => claims.find((x) => x.id === id)!;
    for (const x of claims) Object.assign(x, { corroborated_by: [], contradicted_by: [] });
    const [a1, a2] = q1.claims;
    const [b1, b2] = q2.claims;
    c(a1).corroborated_by = [b1];
    c(b1).corroborated_by = [a1];
    c(a2).contradicted_by = [b2];
    c(a1).corroborated_by.push(a2);
    const found = links(run, steps(run)).filter((l) => l.kind === "corroborates" || l.kind === "contradicts");
    const [from, to] = [Math.min(q1.id, q2.id), Math.max(q1.id, q2.id)];
    expect(found).toEqual([
      { from: `q:${from}`, to: `q:${to}`, kind: "corroborates", count: 1 },
      { from: `q:${from}`, to: `q:${to}`, kind: "contradicts", count: 1 },
    ]);
  });
});

const ev = (seq: number, stage: string, extra: Partial<Progress> = {}): Progress => ({ stage, detail: `event ${seq}`, at: seq, seq, ...extra });
const synthetic = (events: Progress[], ended_at: number | null) =>
  ({
    stage: "research",
    ended_at,
    planned_stages: ["research"],
    stages: [{ name: "research", started_at: 0, ended_at: null, seconds: 0, spans: [] }],
    events,
    calls: [],
    searches: [],
    questions: [{ id: 1, text: "why", title: "", parent: null, depth: 0, status: "open", claims: [], queries: [], missing: "" }],
    result: null,
  }) as unknown as RunDetail;

describe("steps, synthetic", () => {
  it("marks an attempt with no result current while the run is live", () => {
    const run = synthetic([ev(0, "research", { kind: "attempt", question: 1, search: 0 })], null);
    const attempt = find(steps(run), "e:0");
    expect(attempt.state).toBe("current");
    expect(find(steps(run), "q:1").state).toBe("current");
    expect(find(steps(synthetic(run.events, 5)), "e:0").state).toBe("done");
  });

  it("falls back to events under stages for a run with no kinds", () => {
    const run = synthetic([ev(0, "research"), ev(1, "research")], 5);
    run.questions = [];
    const [research] = steps(run);
    expect(walk(steps(run)).map((s) => s.kind)).toEqual(["stage", "event", "event"]);
    expect(research.children.map((s) => s.id)).toEqual(["e:0", "e:1"]);
  });
});
