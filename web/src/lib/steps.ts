import { isResearch, type Progress, type ResearchQuestion, type RunDetail } from "./api";

export type StepKind = "stage" | "question" | "attempt" | "page" | "part" | "section" | "verdict" | "event";
export interface Step {
  id: string;
  kind: StepKind;
  // Short, for a card.
  label: string;
  // Full text, for hover or a panel.
  detail: string;
  state: "done" | "current" | "pending";
  started_at: number | null;
  ended_at: number | null;
  // LmCall ids whose `event` is one of this step's own events, not a descendant's.
  calls: number[];
  // SearchCall id.
  search: number | null;
  // A question's status; null for any other step.
  status: string | null;
  // A number for the card badge: claims added, results, parts, counter progress.
  count: number | null;
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
  const searchResults = new Map(run.searches.map((search) => [search.id, search.results]));

  const research = run.result && isResearch(run.result) ? run.result : null;
  // A run stored before questions were followed live still has them in its result.
  const questions: ResearchQuestion[] = run.questions.length > 0 ? run.questions : (research?.questions ?? []);
  const questionById = new Map(questions.map((q) => [q.id, q]));

  const loose = new Map<string, Step[]>();
  const attempts = new Map<number, Attempt>();
  const attemptsOf = new Map<number, Step[]>();
  const add = (stage: string, child: Step) => loose.set(stage, [...(loose.get(stage) ?? []), child]);

  let counter: Step | null = null;
  let counterStage = "";
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

    if (kind !== null || ev.done == null) counter = null;

    if (kind === "attempt") {
      const s = own(step(`e:${seq}`, "attempt", `Search ${attempts.size + 1}`, ev.detail, state));
      s.search = ev.search ?? null;
      s.count = ev.search == null ? null : (searchResults.get(ev.search) ?? null);
      attempts.set(seq, { step: s, pages: new Map(), resulted: false });
      const list = ev.question == null ? undefined : attemptsOf.get(ev.question);
      if (ev.question != null && questionById.has(ev.question)) attemptsOf.set(ev.question, [...(list ?? []), s]);
      else add(ev.stage, s);
    } else if (kind === "part" && parent) {
      // The page title is the text after the last ": " of a part's detail, which the service gives no other way.
      const page = ev.page ?? 0;
      let p = parent.pages.get(page);
      if (!p) {
        const title = ev.detail.includes(": ") ? ev.detail.slice(ev.detail.lastIndexOf(": ") + 2) : ev.detail;
        p = step(`p:${ev.parent}:${page}`, "page", title, ev.detail);
        parent.pages.set(page, p);
        parent.step.children.push(p);
      }
      p.children.push(own(step(`e:${seq}`, "part", `Part ${p.children.length + 1}`, ev.detail, state)));
      p.count = p.children.length;
    } else if (kind === "result" && parent) {
      parent.resulted = true;
      parent.step.children.push(own(step(`e:${seq}`, "event", ev.detail, ev.detail, state)));
    } else if (kind === "section") {
      const q = ev.question == null ? undefined : questionById.get(ev.question);
      add(ev.stage, own(step(`e:${seq}`, "section", q ? q.title || q.text : ev.detail, ev.detail, state)));
    } else if (kind === null && ev.done != null) {
      // Progress counters of one stage fold into the first, which shows the latest.
      if (counter && counterStage === ev.stage) {
        Object.assign(counter, { label: ev.detail, detail: ev.detail, state, ended_at: end, count: ev.done });
        counter.calls.push(...calls);
      } else {
        counter = own(step(`e:${seq}`, "event", ev.detail, ev.detail, state));
        counter.count = ev.done;
        counterStage = ev.stage;
        add(ev.stage, counter);
      }
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
    span(a);
  }

  const kids = new Map<number, ResearchQuestion[]>();
  const roots: ResearchQuestion[] = [];
  for (const q of questions) {
    if (q.parent !== null && questionById.has(q.parent)) kids.set(q.parent, [...(kids.get(q.parent) ?? []), q]);
    else roots.push(q);
  }
  const question = (q: ResearchQuestion): Step => {
    const s = step(`q:${q.id}`, "question", q.title || q.text, q.text);
    s.count = q.claims.length;
    s.status = q.status;
    s.children = [...(attemptsOf.get(q.id) ?? []), ...(kids.get(q.id) ?? []).map(question)];
    // Answered is done; otherwise done once the run ended, current while an attempt or sub-question is, else pending.
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
    const s = step(`stage:${name}`, "stage", name, name, name === run.stage && live ? "current" : t ? "done" : "pending");
    s.started_at = t?.started_at ?? null;
    s.ended_at = t?.ended_at ?? null;
    s.children = [...(loose.get(name) ?? []), ...(name === "research" ? roots.map(question) : []), ...(name === "verify" ? verdicts() : [])];
    return s;
  });
}
