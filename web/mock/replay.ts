// A stored run as it looked at an earlier moment, with its times moved onto the real clock. The
// page computes clocks from `Date.now()`, so a fixture's own timestamps would read as days old.
import type { components } from "../src/lib/schema";

type RunDetail = components["schemas"]["RunDetail"];
type Usage = components["schemas"]["RunUsage"];

export interface Timing {
  // The fixture's moment on show, in its own clock.
  at: number;
  // Fixture time to real time.
  toReal: (t: number) => number;
  // Fixture seconds to the seconds shown, when the replay runs faster than the run did.
  speed: number;
}

function scaled<T>(value: T, share: number): T {
  if (typeof value === "number") {
    return (Number.isInteger(value) ? Math.round(value * share) : value * share) as T;
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, inner]) => [key, scaled(inner, share)]),
    ) as T;
  }
  return value;
}

type Event = RunDetail["events"][number];

// The service sends a verify counter as one series: every update reuses the first one's `seq` and
// `at`, with its own time in `updated_at`. The fixtures were recorded before that, one event per
// update, so the series is made here. Returns each event as sent, in order, and the calls moved
// onto the series' `seq`.
function sent(run: RunDetail): { sends: Event[]; head: Map<number, number> } {
  const open = new Map<string, Event>();
  const head = new Map<number, number>();
  const sends = run.events.map((event) => {
    if (event.stage !== "verify" || event.kind != null || event.done == null || event.seq == null) return event;
    const first = open.get(event.stage);
    // As the service does, a finished series takes no more updates: the next counter is an event of its own.
    if (!first || (first.total != null && first.done! >= first.total)) {
      open.set(event.stage, event);
      return { ...event, series: event.stage, updated_at: event.at ?? null };
    }
    head.set(event.seq, first.seq!);
    open.set(event.stage, { ...first, done: event.done, total: event.total });
    return { ...event, seq: first.seq, at: first.at, series: event.stage, updated_at: event.at ?? null };
  });
  return { sends, head };
}

export function snapshot(run: RunDetail, { at, toReal, speed }: Timing): RunDetail {
  const start = run.started_at ?? run.created_at;
  const end = run.ended_at ?? at;
  const over = run.ended_at !== null && at >= run.ended_at;
  if (over) at = end;
  const shift = (t: number | null) => (t === null ? null : toReal(t));
  const open = (t: number | null) => (t === null || t > at ? null : toReal(t));

  const { sends, head } = sent(run);
  // `at` is optional in the schema only because the service defaults it.
  const real = sends
    .filter((event) => (event.updated_at ?? event.at ?? 0) <= at)
    .map((event) => ({
      ...event,
      at: toReal(event.at ?? 0),
      ...(event.updated_at == null ? {} : { updated_at: toReal(event.updated_at) }),
    }));
  // A snapshot holds a series once, at its first place, as of its latest update.
  const events = [...new Map(real.map((event) => [event.seq ?? Symbol(), event] as const)).values()];
  const stages = run.stages
    .filter((stage) => stage.started_at <= at)
    .map((stage) => {
      const spans = stage.spans.filter((span) => span.started_at <= at);
      return {
        ...stage,
        started_at: toReal(stage.started_at),
        ended_at: open(stage.ended_at),
        spans: spans.map((span) => ({ started_at: toReal(span.started_at), ended_at: open(span.ended_at) })),
        seconds:
          spans.reduce((sum, span) => sum + Math.min(span.ended_at ?? at, at) - span.started_at, 0) /
          speed,
      };
    });
  const calls = run.calls
    .filter((call) => call.started_at <= at)
    .map((call) => ({ ...call, event: call.event == null ? call.event : (head.get(call.event) ?? call.event) }))
    .map((call) =>
      call.ended_at !== null && call.ended_at <= at
        ? { ...call, started_at: toReal(call.started_at), ended_at: toReal(call.ended_at) }
        : { ...call, started_at: toReal(call.started_at), ended_at: null, prompt_tokens: null, completion_tokens: null },
    );
  const searches = run.searches
    .filter((search) => search.started_at <= at)
    .map((search) =>
      search.ended_at !== null && search.ended_at <= at
        ? { ...search, started_at: toReal(search.started_at), ended_at: toReal(search.ended_at) }
        : { ...search, started_at: toReal(search.started_at), ended_at: null, results: null, failures: [] },
    );
  // Usage has no timeline, so it grows with the share of the run gone by.
  const share = over || end === start ? 1 : (at - start) / (end - start);
  // Nor have questions, so they appear in the order asked, likewise.
  const questions = run.questions.slice(0, Math.ceil(run.questions.length * share));
  const usage: Usage = scaled(run.usage, share);
  const seen = new Set(stages.map((stage) => stage.name));
  usage.by_stage = Object.fromEntries(
    Object.entries(usage.by_stage).filter(([name]) => seen.has(name) || name === "unstaged"),
  );
  // The one sent last, which is not always the last in order: a series update is sent late.
  const latest = real.at(-1) ?? null;

  return {
    ...run,
    created_at: toReal(run.created_at),
    started_at: shift(run.started_at),
    ended_at: over ? shift(run.ended_at) : null,
    state: over ? run.state : "running",
    stage: over ? run.stage : (latest?.stage ?? null),
    error: over ? run.error : null,
    latest,
    events,
    stages,
    calls,
    searches,
    questions,
    usage,
    result: over ? run.result : null,
    has_result: over && run.result !== null,
  };
}
