import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { isResearch, type Progress, type RunDetail } from "./api";
import { steps, type Step } from "./steps";

const graph: RunDetail = JSON.parse(readFileSync(join(import.meta.dirname, "..", "..", "fixtures", "graph.json"), "utf8"));

const walk = (list: Step[]): Step[] => list.flatMap((s) => [s, ...walk(s.children)]);
const find = (list: Step[], id: string) => walk(list).find((s) => s.id === id)!;

describe("steps", () => {
  const roots = steps(graph);
  const stage = (name: string) => roots.find((s) => s.id === `stage:${name}`)!;

  it("has a root per stage, in order", () => {
    expect(roots.map((s) => s.label)).toEqual(["brief", "research", "write", "verify"]);
  });

  it("holds the root questions under research", () => {
    const rootQuestions = graph.questions.filter((q) => q.parent === null).map((q) => `q:${q.id}`);
    expect(stage("research").children.filter((s) => s.kind === "question").map((s) => s.id)).toEqual(rootQuestions);
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
