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

export function snapshot(run: RunDetail, { at, toReal, speed }: Timing): RunDetail {
  const start = run.started_at ?? run.created_at;
  const end = run.ended_at ?? at;
  const over = run.ended_at !== null && at >= run.ended_at;
  if (over) at = end;
  const shift = (t: number | null) => (t === null ? null : toReal(t));
  const open = (t: number | null) => (t === null || t > at ? null : toReal(t));

  // `at` is optional in the schema only because the service defaults it.
  const events = run.events
    .filter((event) => (event.at ?? 0) <= at)
    .map((event) => ({ ...event, at: toReal(event.at ?? 0) }));
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
    .map((call) =>
      call.ended_at !== null && call.ended_at <= at
        ? { ...call, started_at: toReal(call.started_at), ended_at: toReal(call.ended_at) }
        : { ...call, started_at: toReal(call.started_at), ended_at: null, prompt_tokens: null, completion_tokens: null },
    );
  // Usage has no timeline, so it grows with the share of the run gone by.
  const share = over || end === start ? 1 : (at - start) / (end - start);
  const usage: Usage = scaled(run.usage, share);
  const seen = new Set(stages.map((stage) => stage.name));
  usage.by_stage = Object.fromEntries(
    Object.entries(usage.by_stage).filter(([name]) => seen.has(name) || name === "unstaged"),
  );
  const latest = events.at(-1) ?? null;

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
    usage,
    result: over ? run.result : null,
    has_result: over && run.result !== null,
  };
}
