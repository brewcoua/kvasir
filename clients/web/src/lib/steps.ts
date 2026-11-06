import type { IconName } from "../components/Icon.svelte";
import { isResearch, type Progress, type ResearchQuestion, type RunDetail } from "./api";
import { duration } from "./format";

export type StepKind = "stage" | "question" | "attempt" | "recall" | "repair" | "page" | "part" | "section" | "verdict" | "split" | "event";
export interface Step {
  id: string;
  kind: StepKind;
  // Short, for a card.
  label: string;
  // Full text, for hover or a panel.
  detail: string;
  state: "done" | "current" | "pending" | "skipped";
  started_at: number | null;
  ended_at: number | null;
  // LmCall ids whose `event` is one of this step's own events, not a descendant's.
  calls: number[];
  // SearchCall id.
  search: number | null;
  // A question's status; null for any other step.
  status: string | null;
  // A number for the card badge: claims added, results, parts, counter progress. A recall's is its pieces.
  count: number | null;
  // An attempt's or recall's claims added, from its result event; null for any other step.
  added: number | null;
  // An attempt's number among the run's searches; null for any other step.
  index: number | null;
  // An attempt's query, once its search has started; null for any other step.
  query: string | null;
  // The pages an attempt has read, or a recall's pages; null for any other step.
  read: number | null;
  // A stage's latest progress counter; null for any other step or a stage with none.
  progress: { done: number; total: number | null; detail: string; at: number | null; updated_at: number | null; kind: string | null } | null;
  // A stage's other plain events, in order; they get no card.
  log: { seq: number; at: number | null; detail: string }[];
  // A question's depth; null for any other step.
  depth: number | null;
  // The question a question or section step belongs to; a repair's question has no ResearchQuestion.
  question: number | null;
  // A question's claims, in its own order; empty for any other step.
  claims: { id: number; confidence: "high" | "medium" | "low" }[];
  children: Step[];
}

const step = (id: string, kind: StepKind, label: string, detail = label, state: Step["state"] = "done"): Step => ({
  id,
  kind,
  label,
  detail,
  state,
  started_at: null,
  ended_at: null,
  calls: [],
  search: null,
  status: null,
  count: null,
  added: null,
  index: null,
  query: null,
  read: null,
  progress: null,
  log: [],
  depth: null,
  question: null,
  claims: [],
  children: [],
});

// A parent spans its children where their times are known; a current one is still open.
function span(parent: Step) {
  const known = parent.children.flatMap((child) => (child.started_at === null ? [] : [child]));
  if (known.length === 0) return;
  parent.started_at = Math.min(...known.map((child) => child.started_at!), parent.started_at ?? Infinity);
  if (parent.state !== "current") {
    const ends = parent.children.flatMap((child) => child.ended_at ?? child.started_at ?? []);
    parent.ended_at = Math.max(...ends, parent.ended_at ?? -Infinity);
  }
}

// Time order, steps with no time last; sort is stable, so ties keep their given order.
const byTime = (a: Step, b: Step) => (a.started_at === b.started_at ? 0 : a.started_at === null ? 1 : b.started_at === null ? -1 : a.started_at - b.started_at);

// A planned stage that never ran though the run went past it: one the options or the question left out, such as
// ranking for a report. A failed run's stages after the failure were not reached, and stay pending.
export function skipped(run: RunDetail, name: string): boolean {
  if (run.stages.some((stage) => stage.name === name)) return false;
  const names = [...run.planned_stages, ...run.stages.map((stage) => stage.name).filter((n) => !run.planned_stages.includes(n))];
  const later = names.slice(names.indexOf(name) + 1);
  return run.state === "done" || run.stages.some((stage) => later.includes(stage.name));
}

// The page title is the text after the last ": " of a page's or part's detail.
const pageTitle = (detail: string) => (detail.includes(": ") ? detail.slice(detail.lastIndexOf(": ") + 2) : detail);

// What a repair's question was asked, from the detail of the first attempt or recall under it.
const repairText = (detail: string) => detail.match(/, for: (.+)$/s)?.[1] ?? detail.match(/^recall for (.+): \d+ pieces from \d+ pages$/s)?.[1] ?? null;

interface Attempt {
  step: Step;
  pages: Map<number, Step>;
  resulted: boolean;
}

export function steps(run: RunDetail): Step[] {
  const live = run.ended_at === null;
  const events = run.events;
  const lastAt = run.ended_at;

  const callsBy = new Map<number, number[]>();
  for (const call of run.calls) if (call.event != null && call.id != null) callsBy.set(call.event, [...(callsBy.get(call.event) ?? []), call.id]);
  const searchById = new Map(run.searches.map((search) => [search.id, search]));
  // The service starts a search after publishing its attempt, so the attempt cannot name it; the search names the event before it.
  const searchAfter = new Map(run.searches.flatMap((search) => (search.event == null ? [] : [[search.event, search.id] as const])));

  const research = run.result && isResearch(run.result) ? run.result : null;
  // A run stored before questions were followed live still has them in its result.
  const questions: ResearchQuestion[] = run.questions.length > 0 ? run.questions : (research?.questions ?? []);
  const questionById = new Map(questions.map((q) => [q.id, q]));

  const loose = new Map<string, Step[]>();
  // Attempts and recalls both hold pages, and take a result.
  const attempts = new Map<number, Attempt>();
  let searched = 0;
  // A repair's own events have no ResearchQuestion, so what it asked is kept from the events under it.
  const repairs = new Map<number, { step: Step; rows: { question: number; step: Step }[]; texts: Map<number, string> }>();
  const attemptsOf = new Map<number, Step[]>();
  const add = (stage: string, child: Step) => loose.set(stage, [...(loose.get(stage) ?? []), child]);

  // What the stages keep of the events that get no card.
  const progressOf = new Map<string, Step["progress"]>();
  const logOf = new Map<string, Step["log"]>();
  const callsOf = new Map<string, number[]>();
  const keepCalls = (stage: string, calls: number[]) => callsOf.set(stage, [...(callsOf.get(stage) ?? []), ...calls]);
  const counter = (ev: Progress): NonNullable<Step["progress"]> => ({
    done: ev.done!,
    total: ev.total ?? null,
    detail: ev.detail,
    at: ev.at ?? null,
    updated_at: ev.updated_at ?? null,
    kind: ev.kind ?? null,
  });
  events.forEach((ev: Progress, i) => {
    const seq = ev.seq ?? i;
    const at = ev.at ?? null;
    const next = events[i + 1];
    const end = next ? (next.at ?? null) : live ? null : lastAt;
    const state = i === events.length - 1 && live ? "current" : "done";
    const calls = callsBy.get(seq) ?? [];
    const kind = ev.kind ?? null;
    const parent = ev.parent == null ? undefined : attempts.get(ev.parent);
    const own = (s: Step) => Object.assign(s, { started_at: at, ended_at: end, calls });

    if (kind === "attempt" || kind === "recall") {
      const s = own(step(`e:${seq}`, kind, "Recall", ev.detail, state));
      if (kind === "attempt") {
        s.label = `Search ${++searched}`;
        s.index = searched;
        s.search = ev.search ?? searchAfter.get(seq) ?? null;
        const search = s.search == null ? undefined : searchById.get(s.search);
        s.count = search?.results ?? null;
        s.query = search?.query ?? null;
      } else {
        s.count = ev.done ?? null;
        s.read = ev.total ?? null;
      }
      attempts.set(seq, { step: s, pages: new Map(), resulted: false });
      // Only a top-level search feeds the stage's bar: one under a repair has its own count, which
      // would take the place of the verify counter.
      if (kind === "attempt" && ev.parent == null && ev.done != null && ev.total != null) progressOf.set(ev.stage, counter(ev));
      const repair = ev.parent == null ? undefined : repairs.get(ev.parent);
      const list = ev.question == null ? undefined : attemptsOf.get(ev.question);
      if (repair && ev.question != null) {
        repair.rows.push({ question: ev.question, step: s });
        const text = repairText(ev.detail);
        if (text && !repair.texts.has(ev.question)) repair.texts.set(ev.question, text);
      } else if (ev.question != null && questionById.has(ev.question)) attemptsOf.set(ev.question, [...(list ?? []), s]);
      else add(ev.stage, s);
    } else if (kind === "repair") {
      const s = own(step(`e:${seq}`, "repair", "Repair", ev.detail, state));
      repairs.set(seq, { step: s, rows: [], texts: new Map() });
      add(ev.stage, s);
    } else if ((kind === "page" || kind === "part") && ev.parent != null && !parent) {
      // Its attempt was evicted, so there is nothing to put it under.
    } else if (kind === "page" && parent) {
      const page = ev.page ?? 0;
      if (!parent.pages.has(page)) {
        const p = own(step(`p:${ev.parent}:${page}`, "page", pageTitle(ev.detail), ev.detail));
        parent.pages.set(page, p);
        parent.step.children.push(p);
      }
    } else if (kind === "part" && parent) {
      const page = ev.page ?? 0;
      let p = parent.pages.get(page);
      if (!p) {
        p = step(`p:${ev.parent}:${page}`, "page", pageTitle(ev.detail), ev.detail);
        parent.pages.set(page, p);
        parent.step.children.push(p);
      }
      p.children.push(own(step(`e:${seq}`, "part", `Part ${p.children.length + 1}`, ev.detail, state)));
      p.count = p.children.length;
    } else if (kind === "result") {
      if (parent) {
        parent.resulted = true;
        parent.step.added = ev.done ?? null;
        parent.step.calls.push(...calls);
      } else {
        logOf.set(ev.stage, [...(logOf.get(ev.stage) ?? []), { seq, at, detail: ev.detail }]);
        keepCalls(ev.stage, calls);
      }
    } else if (kind === "split") {
      add(ev.stage, own(step(`e:${seq}`, "split", "Split", ev.detail, state)));
    } else if (kind === "section") {
      const q = ev.question == null ? undefined : questionById.get(ev.question);
      const s = own(step(`e:${seq}`, "section", q ? q.title || q.text : ev.detail.replace(/^writing /, ""), ev.detail, state));
      s.question = ev.question ?? null;
      add(ev.stage, s);
      if (ev.done != null) progressOf.set(ev.stage, counter(ev));
    } else if (kind === null) {
      if (ev.done != null) progressOf.set(ev.stage, counter(ev));
      else logOf.set(ev.stage, [...(logOf.get(ev.stage) ?? []), { seq, at, detail: ev.detail }]);
      keepCalls(ev.stage, calls);
    } else {
      const s = own(step(`e:${seq}`, "event", ev.detail, ev.detail, state));
      s.search = ev.search ?? null;
      add(ev.stage, s);
    }
  });

  for (const { step: a, pages, resulted } of attempts.values()) {
    a.state = resulted || !live ? "done" : "current";
    if (a.state === "current") a.ended_at = null;
    // The last part of the last page is the one in progress.
    if (a.state === "current") {
      const parts = [...pages.values()].at(-1)?.children;
      if (parts?.length) parts[parts.length - 1].state = "current";
    }
    for (const p of pages.values()) {
      p.state = p === [...pages.values()].at(-1) && a.state === "current" ? "current" : "done";
      span(p);
    }
    if (a.kind === "attempt") a.read = pages.size;
    span(a);
  }

  for (const { step: repair, rows, texts } of repairs.values()) {
    const ids = [...new Set(rows.map((row) => row.question))];
    repair.children = ids.map((id) => {
      const q = step(`q:${id}`, "question", texts.get(id) ?? `Question ${id}`);
      q.question = id;
      q.depth = 0;
      q.children = rows.filter((row) => row.question === id).map((row) => row.step).sort(byTime);
      q.count = q.children.reduce((n, child) => n + (child.added ?? 0), 0);
      q.state = !live ? "done" : q.children.some((child) => child.state === "current") ? "current" : "done";
      span(q);
      return q;
    });
    repair.count = ids.length;
    if (!live) repair.state = "done";
    else if (repair.children.some((child) => child.state === "current")) repair.state = "current";
    span(repair);
  }

  const confidence = new Map((research?.claims ?? []).map((c) => [c.id, c.confidence]));
  const question = (q: ResearchQuestion): Step => {
    const s = step(`q:${q.id}`, "question", q.title || q.text, q.text);
    s.count = q.claims.length;
    s.status = q.status;
    s.depth = q.depth;
    s.question = q.id;
    s.claims = q.claims.flatMap((id) => (confidence.has(id) ? [{ id, confidence: confidence.get(id)! }] : []));
    s.children = (attemptsOf.get(q.id) ?? []).sort(byTime);
    // Answered is done; otherwise done once the run ended, current while an attempt is, else pending.
    s.state = q.status === "answered" || !live ? "done" : s.children.some((child) => child.state === "current") ? "current" : "pending";
    span(s);
    return s;
  };

  const verdicts = (): Step[] => {
    if (!research) return [];
    const tiers = new Map<string, number>();
    for (const r of research.removed) tiers.set(r.stage, (tiers.get(r.stage) ?? 0) + 1);
    const verdict = (id: string, label: string, count: number) => Object.assign(step(`v:${id}`, "verdict", label, `${count} ${label}`), { count });
    return [
      ...[...tiers].map(([tier, n]) => verdict(tier, `removed at ${tier}`, n)),
      ...(research.revised.length > 0 ? [verdict("rewritten", "rewritten", research.revised.length)] : []),
      ...(research.unverified.length > 0 ? [verdict("unverified", "unverified", research.unverified.length)] : []),
    ];
  };

  // Planned stages in order, then any the plan did not name, as Detail.svelte lists them.
  const timing = new Map(run.stages.map((stage) => [stage.name, stage]));
  const names = [...run.planned_stages];
  for (const name of [...timing.keys(), ...events.map((ev) => ev.stage)]) if (!names.includes(name)) names.push(name);

  return names.map((name) => {
    const t = timing.get(name);
    const s = step(`stage:${name}`, "stage", name, name, name === run.stage && live ? "current" : t ? "done" : skipped(run, name) ? "skipped" : "pending");
    s.started_at = t?.started_at ?? null;
    s.ended_at = t?.ended_at ?? null;
    s.progress = progressOf.get(name) ?? null;
    s.log = logOf.get(name) ?? [];
    s.calls = callsOf.get(name) ?? [];
    s.children = [...(loose.get(name) ?? []), ...(name === "verify" ? verdicts() : [])].sort(byTime);
    // The view lays questions out in columns by depth.
    if (name === "research") s.children.push(...questions.map(question).sort((a, b) => a.depth! - b.depth! || a.question! - b.question!));
    return s;
  });
}

// Nothing to show below its heading, so a card is the heading alone. A stage's stats and a question's claims are all
// that ever fill a body, and a skipped stage has neither.
export const isShort = (step: Step) => step.state === "skipped" || (step.kind !== "stage" && step.kind !== "question");

// A verdict's id is `v:<tier>`; any tier other than the two below removed sentences.
export const outcome = (step: Step): "removed" | "rewritten" | "unverified" => {
  const tier = step.id.slice(2);
  return tier === "rewritten" || tier === "unverified" ? tier : "removed";
};

// Any stage not named here gets the generic glyph.
const STAGE_ICONS: Record<string, IconName> = {
  brief: "brief",
  background: "background",
  research: "search",
  assess: "assess",
  write: "write",
  verify: "verify",
  outline: "outline",
  article: "page",
  polish: "polish",
};
const OUTCOME_ICONS = { removed: "close", rewritten: "brief", unverified: "question" } as const;
const KIND_ICONS: Partial<Record<StepKind, IconName>> = { question: "question", attempt: "search", recall: "page", repair: "verify", page: "page", section: "write", split: "split" };

export function icon(step: Step): IconName {
  if (step.kind === "stage") return STAGE_ICONS[step.label] ?? "stage";
  if (step.kind === "verdict") return OUTCOME_ICONS[outcome(step)];
  return KIND_ICONS[step.kind] ?? "event";
}

export const progressText = (p: NonNullable<Step["progress"]>) => (p.total ? `${p.done}/${p.total}` : String(p.done));

// A step's progress or an event: the first holds `at` as null, the schema leaves it out.
export type Counter = Pick<Progress, "done" | "total" | "updated_at" | "kind" | "parent"> & { at?: number | null };

// A count through a stage, with a total to measure it against. A recall's and a result's done and total count
// other things, and an attempt under a repair counts the repair's searches, not the stage's.
export const isCounter = (ev: Counter) => ev.done != null && !!ev.total && (ev.kind == null || ev.kind === "section" || (ev.kind === "attempt" && ev.parent == null));

// Seconds left at the pace so far: from the series' first event to its latest update, less the time since that
// update. None while too few are done to know the pace, for a single event, and once the count is finished.
export function remaining(ev: Counter, now: number): number | null {
  const [done, total] = [ev.done!, ev.total!];
  if (ev.updated_at == null || ev.at == null || done < 3 || done >= total) return null;
  const left = ((ev.updated_at - ev.at) / done) * (total - done) - (now - ev.updated_at);
  return left > 0 ? left : null;
}

// Stages finished of those the run goes through, whole stages only: a stage's counters restart, as verify's do, and
// counting them in would let the share go back. Null for a run with no plan.
export function overall(run: RunDetail): { done: number; total: number } | null {
  if (run.planned_stages.length === 0) return null;
  const live = run.ended_at === null;
  const names = [...run.planned_stages, ...run.stages.map((s) => s.name).filter((n) => !run.planned_stages.includes(n))].filter((n) => !skipped(run, n));
  const done = names.filter((n) => run.stages.some((s) => s.name === n) && !(live && n === run.stage)).length;
  return { done, total: names.length };
}

// The current stage's latest count, as the Steps view takes it.
export const stageCounter = (run: RunDetail): Progress | null => [...run.events].reverse().find((ev) => ev.stage === run.stage && isCounter(ev)) ?? null;

// Floored, so 100% means finished. The estimate only while the run is live.
export function meter(ev: Counter, now: number, live: boolean): { share: number; text: string } {
  const share = Math.min(ev.done! / ev.total!, 1);
  const left = live ? remaining(ev, now) : null;
  return { share, text: `${Math.floor(share * 100)}% · ${ev.done}/${ev.total}${left === null ? "" : ` · ~${duration(Math.round(left))}`}` };
}

export interface Link {
  from: string;
  to: string;
  kind: "raised" | "drew" | "removed" | "corroborates" | "contradicts";
  count: number;
}

export function links(run: RunDetail, roots: Step[]): Link[] {
  const all: Step[] = [];
  const collect = (list: Step[]) => list.forEach((s) => (all.push(s), collect(s.children)));
  collect(roots);
  const ids = new Set(all.map((s) => s.id));
  const out: Link[] = [];
  const link = (from: string, to: string, kind: Link["kind"], count = 1) => {
    if (count > 0 && ids.has(from) && ids.has(to)) out.push({ from, to, kind, count });
  };

  const research = run.result && isResearch(run.result) ? run.result : null;
  const questions = run.questions.length > 0 ? run.questions : (research?.questions ?? []);
  const split = all.find((s) => s.kind === "split");
  for (const q of questions) {
    if (q.parent !== null) link(`q:${q.parent}`, `q:${q.id}`, "raised");
    else if (split) link(split.id, `q:${q.id}`, "raised");
  }

  const textOf = new Map(questions.map((q) => [q.id, q.text]));
  for (const s of all) {
    if (s.kind !== "section" || s.question === null) continue;
    link(`q:${s.question}`, s.id, "drew");
    const tiers = new Map<string, number>();
    for (const r of research?.removed ?? []) if (r.section === textOf.get(s.question)) tiers.set(r.stage, (tiers.get(r.stage) ?? 0) + 1);
    for (const [tier, n] of tiers) link(s.id, `v:${tier}`, "removed", n);
  }

  const questionOf = new Map<number, number>();
  for (const q of questions) for (const id of q.claims) if (!questionOf.has(id)) questionOf.set(id, q.id);
  const between = new Map<string, Link>();
  const seen = new Set<string>();
  for (const [kind, key] of [["corroborates", "corroborated_by"], ["contradicts", "contradicted_by"]] as const) {
    for (const c of research?.claims ?? []) {
      for (const other of c[key] ?? []) {
        const [a, b] = [questionOf.get(c.id), questionOf.get(other)];
        const pair = `${kind}:${Math.min(c.id, other)}:${Math.max(c.id, other)}`;
        if (a === undefined || b === undefined || a === b || seen.has(pair)) continue;
        seen.add(pair);
        const from = Math.min(a, b);
        const to = Math.max(a, b);
        const l = between.get(`${kind}:${from}:${to}`) ?? { from: `q:${from}`, to: `q:${to}`, kind, count: 0 };
        l.count++;
        between.set(`${kind}:${from}:${to}`, l);
      }
    }
  }
  for (const l of between.values()) link(l.from, l.to, l.kind, l.count);
  return out;
}
