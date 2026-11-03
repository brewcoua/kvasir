import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { isResearch, type Progress, type RunDetail } from "./api";
import { layout } from "./layout";
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
    expect(attempt.children.every((c) => c.kind === "page")).toBe(true);
    const result = graph.events.find((e) => e.kind === "result" && e.parent === Number(attempt.id.slice(2)))!;
    expect(attempt.added).toBe(result.done ?? null);
  });

  it("puts every call on exactly one step", () => {
    const ids = walk(roots).flatMap((s) => s.calls);
    const sorted = (list: (number | null | undefined)[]) => list.map(Number).sort((a, b) => a - b);
    expect(sorted(ids)).toEqual(sorted(graph.calls.map((c) => c.id)));
  });

  it("keeps verify progress counters on the stage, not as cards", () => {
    expect(stage("verify").children.filter((s) => s.kind === "event")).toHaveLength(0);
    const last = graph.events.filter((e) => e.stage === "verify" && !e.kind && e.done != null).at(-1)!;
    expect(stage("verify").progress).toEqual({ done: last.done, total: last.total ?? null, detail: last.detail });
  });

  it("keeps a plain event in the stage's log, with no card", () => {
    expect(stage("brief").children).toEqual([]);
    expect(stage("brief").log.map((l) => l.detail)).toEqual(["writing the research brief"]);
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

  it("finds an attempt's search by the event the search names, when the attempt names none", () => {
    const run = synthetic([ev(0, "research", { kind: "attempt", question: 1 })], 5);
    run.searches = [{ id: 7, event: 0, results: 12 }] as unknown as RunDetail["searches"];
    const attempt = find(steps(run), "e:0");
    expect(attempt.search).toBe(7);
    expect(attempt.count).toBe(12);
  });

  it("logs the events of a run with no kinds under their stage, without cards", () => {
    const run = synthetic([ev(0, "research"), ev(1, "research"), ev(2, "research", { done: 1, total: 4 })], 5);
    run.questions = [];
    const [research] = steps(run);
    expect(walk(steps(run)).map((s) => s.kind)).toEqual(["stage"]);
    expect(research.log.map((l) => l.seq)).toEqual([0, 1]);
    expect(research.progress).toEqual({ done: 1, total: 4, detail: "event 2" });
  });

  it("puts a page event under its attempt, and a result on the attempt", () => {
    const run = synthetic(
      [
        ev(0, "research", { kind: "attempt", question: 1, search: 0 }),
        ev(1, "research", { kind: "page", parent: 0, page: 1, detail: "read page 1/2: First title" }),
        ev(2, "research", { kind: "page", parent: 0, page: 2, detail: "read page 2/2: Other: title" }),
        ev(3, "research", { kind: "part", parent: 0, page: 2, detail: "read part 1/2 of page 2: Other: title" }),
        ev(4, "research", { kind: "result", parent: 0, done: 3, total: 9, detail: "3 new claims, 9 in all" }),
      ],
      5,
    );
    const attempt = find(steps(run), "e:0");
    expect(attempt.children.map((c) => [c.id, c.kind, c.label, c.children.length])).toEqual([
      ["p:0:1", "page", "First title", 0],
      ["p:0:2", "page", "title", 1],
    ]);
    expect(attempt.added).toBe(3);
    expect(attempt.state).toBe("done");
    expect(walk(steps(run)).some((s) => s.id === "e:4")).toBe(false);
  });

  it("makes no card for a counter or a log line, and keeps the latest counter and every line in order", () => {
    const run = synthetic(
      [
        ev(0, "research", { done: 1, total: 3 }),
        ev(1, "research", { detail: "first" }),
        ev(2, "research", { done: 2, total: 3 }),
        ev(3, "research", { detail: "second" }),
      ],
      5,
    );
    run.questions = [];
    const [research] = steps(run);
    expect(research.children).toEqual([]);
    expect(research.progress).toEqual({ done: 2, total: 3, detail: "event 2" });
    expect(research.log).toEqual([
      { seq: 1, at: 1, detail: "first" },
      { seq: 3, at: 3, detail: "second" },
    ]);
  });

  it("keeps a real-sized run to a handful of cards", () => {
    const questions = [1, 2, 3].map((id) => ({ id, text: `q${id}`, title: "", parent: null, depth: 0, status: "answered", claims: [], queries: [], missing: "" }));
    const events: Progress[] = [ev(0, "research", { kind: "split" })];
    for (let a = 0; a < 6; a++) {
      const at = events.length;
      events.push(ev(at, "research", { kind: "attempt", question: (a % 3) + 1, search: a }));
      for (const page of [1, 2]) events.push(ev(events.length, "research", { kind: "page", parent: at, page, detail: `read page ${page}/2: title ${page}` }));
      events.push(ev(events.length, "research", { kind: "result", parent: at, done: 2, total: 2 * (a + 1) }));
    }
    for (let i = 0; i < 30; i++) events.push(ev(events.length, "research", { done: i, total: 30 }));
    for (let i = 0; i < 10; i++) events.push(ev(events.length, "research", { detail: `line ${i}` }));
    const run = synthetic(events, 100);
    run.questions = questions as unknown as RunDetail["questions"];
    const roots = steps(run);
    const [stage] = roots;
    expect(stage.children.map((c) => c.kind)).toEqual(["split", "question", "question", "question"]);
    for (const q of stage.children.filter((c) => c.kind === "question")) expect(q.children.map((c) => c.kind)).toEqual(["attempt", "attempt"]);
    expect(stage.log).toHaveLength(10);
    expect(stage.progress).toMatchObject({ done: 29, total: 30 });
    const { nodes } = layout(roots, new Set(walk(roots).map((s) => s.id)), links(run, roots));
    expect(nodes.length).toBeLessThan(40);
  });
});
