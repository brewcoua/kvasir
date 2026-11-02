// The API, answered from `fixtures/`, for `pnpm dev:mock` and the end-to-end tests.
//
// Each fixture is a run under its file name, so `#ranking/result` opens that result. Two more are
// made from the ranking fixture: `running`, frozen partway with its clock still going, and `live`,
// replayed at MOCK_SPEED times its real pace and started again once it ends. Starting a run from
// the page replays a fixture the same way. `<name>.calls.json` holds what a fixture's calls sent
// and answered, by call id, for runs recorded with KVASIR_KEEP_CALL_CONTENT, and
// `<name>.searches.json` what its searches returned, by search id. `graph-live` replays the graph
// fixture the way `live` replays the ranking.
import { readdirSync, readFileSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { join } from "node:path";
import type { Plugin } from "vite";
import type { components } from "../src/lib/schema";
import { snapshot, type Timing } from "./replay.ts";

type RunDetail = components["schemas"]["RunDetail"];
type LmCallContent = components["schemas"]["LmCallContent"];
type SearchContent = components["schemas"]["SearchContent"];

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const SPEED = Number(process.env.MOCK_SPEED ?? 20);
// How far into the ranking run `running` is frozen.
const FROZEN_AT = 0.6;
const PAUSE_SECONDS = 10;
const TICK_MS = 500;
// As many calls as the service puts in a `status` event.
const STATUS_CALLS = 50;

const MODELS = { model_fast: "openai/gpt-oss:20b", model_strong: "openai/gpt-oss:20b" };
// As `GET /v1/methods` answers, with only the defaults of each option's schema.
const method = (name: string, clarifies: boolean, stages: string[], defaults: Record<string, unknown>) => ({
  name,
  clarifies,
  stages,
  options: { properties: Object.fromEntries(Object.entries(defaults).map(([key, value]) => [key, { default: value }])) },
});
const METHODS = [
  method("ledger", true, ["brief", "background", "research", "assess", "write", "verify"], {
    ...MODELS,
    model_writer: "openai/gpt-oss:20b",
    max_searches: 24,
    results_per_query: 5,
    max_attempts: 3,
    follow_ups: true,
    max_depth: 1,
    max_entities: 6,
    max_criteria: 5,
    ground: false,
    ground_intake: false,
    corroborate: false,
    contradict: false,
  }),
  method("graph", true, ["brief", "research", "write", "verify"], {
    ...MODELS,
    model_writer: "openai/gpt-oss:20b",
    research_minutes: 20,
    results_per_query: 5,
    max_attempts: 3,
    max_depth: 4,
    ground: false,
    ground_intake: false,
    corroborate: false,
    contradict: false,
  }),
  method("storm", false, ["research", "outline", "article", "polish"], {
    ...MODELS,
    search_top_k: 3,
    max_conv_turn: 3,
    max_perspective: 3,
    do_polish_article: true,
  }),
];

const QUESTIONS = [
  "Which region or market should the answer cover?",
  "Is there a budget or a time frame that matters?",
];

interface Entry {
  run: RunDetail;
  timing: () => Timing;
  contents?: Record<string, LmCallContent>;
  searches?: Record<string, SearchContent>;
}

const now = () => Date.now() / 1000;

// An ended run needs no shift: the page only shows its durations, never when it happened.
function stored(run: RunDetail): Entry {
  return { run, timing: () => ({ at: run.ended_at ?? run.created_at, toReal: (t) => t, speed: 1 }) };
}

function frozen(run: RunDetail, id: string): Entry {
  const start = run.started_at ?? run.created_at;
  const at = start + ((run.ended_at ?? start) - start) * FROZEN_AT;
  const offset = now() - at;
  return { run: { ...run, id }, timing: () => ({ at, toReal: (t) => t + offset, speed: 1 }) };
}

// `loop` starts the replay again after a pause, for a run that is always in progress.
function replay(run: RunDetail, id: string, loop: boolean, patch: Partial<RunDetail> = {}): Entry {
  const start = run.started_at ?? run.created_at;
  const length = ((run.ended_at ?? start) - start) / SPEED;
  const began = now();
  return {
    run: { ...run, ...patch, id },
    timing: () => {
      const cycle = length + PAUSE_SECONDS;
      const elapsed = now() - began;
      const offset = loop ? Math.floor(elapsed / cycle) * cycle : 0;
      const at = start + (elapsed - offset) * SPEED;
      return { at, toReal: (t) => began + offset + (t - start) / SPEED, speed: SPEED };
    },
  };
}

function load(): Map<string, Entry> {
  const runs = new Map<string, Entry>();
  const files = readdirSync(FIXTURES);
  const kept = (name: string) => (files.includes(name) ? JSON.parse(readFileSync(join(FIXTURES, name), "utf8")) : undefined);
  for (const file of files.filter((name) => /^[^.]+\.json$/.test(name)).sort()) {
    const run: RunDetail = JSON.parse(readFileSync(join(FIXTURES, file), "utf8"));
    const id = file.replace(/\.json$/, "");
    runs.set(id, { ...stored({ ...run, id }), contents: kept(`${id}.calls.json`), searches: kept(`${id}.searches.json`) });
  }
  const ranking = runs.get("ranking")!.run;
  runs.set("running", frozen(ranking, "running"));
  runs.set("live", replay(ranking, "live", true));
  const graph = runs.get("graph")!;
  runs.set("graph-live", { ...replay(graph.run, "graph-live", true), searches: graph.searches });
  return runs;
}

function summary(run: RunDetail) {
  const { events: _events, result: _result, stages: _stages, calls: _calls, ...rest } = run;
  return rest;
}

// The `status` event, which carries only the most recent calls and searches.
function status(run: RunDetail) {
  const { events: _events, result: _result, ...rest } = run;
  return { ...rest, calls: run.calls.slice(-STATUS_CALLS), searches: run.searches.slice(-STATUS_CALLS) };
}

const view = (entry: Entry) => snapshot(entry.run, entry.timing());

function send(response: ServerResponse, status: number, body: unknown) {
  response.statusCode = status;
  response.setHeader("content-type", "application/json");
  response.end(JSON.stringify(body));
}

async function read(request: IncomingMessage): Promise<Record<string, unknown>> {
  let body = "";
  for await (const chunk of request) body += chunk;
  return body ? JSON.parse(body) : {};
}

function stream(response: ServerResponse) {
  response.setHeader("content-type", "text/event-stream");
  response.setHeader("cache-control", "no-cache");
  return (event: string, data: unknown) => response.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

// Like `/v1/runs/{id}/events`: a snapshot, then each event and a status after it, then a last
// snapshot once the run ends.
function follow(entry: Entry, request: IncomingMessage, response: ServerResponse) {
  const frame = stream(response);
  let last = view(entry);
  frame("snapshot", last);
  if (last.ended_at !== null) return response.end();
  const timer = setInterval(() => {
    const next = view(entry);
    // A looping replay that started again.
    if (next.events.length < last.events.length) {
      frame("snapshot", next);
    } else {
      for (const event of next.events.slice(last.events.length)) frame("progress", event);
      frame("status", status(next));
      if (next.ended_at !== null) {
        frame("snapshot", next);
        clearInterval(timer);
        response.end();
      }
    }
    last = next;
  }, TICK_MS);
  request.on("close", () => clearInterval(timer));
}

export function mock(): Plugin {
  const runs = load();
  return {
    name: "kvasir-mock",
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        const url = new URL(request.url ?? "/", "http://mock");
        const path = url.pathname;
        if (!path.startsWith("/v1/")) return next();

        if (request.method === "GET" && path === "/v1/runs") {
          const all = [...runs.values()].map((entry) => summary(view(entry)));
          return send(response, 200, all.sort((a, b) => b.created_at - a.created_at));
        }
        if (request.method === "GET" && path === "/v1/methods") return send(response, 200, METHODS);

        const call = path.match(/^\/v1\/runs\/([^/]+)\/calls\/(\d+)$/);
        if (request.method === "GET" && call) {
          const kept = runs.get(call[1])?.contents?.[call[2]];
          if (!kept) return send(response, 404, { message: `no content kept for call ${call[2]}` });
          return send(response, 200, kept);
        }

        const search = path.match(/^\/v1\/runs\/([^/]+)\/searches\/(\d+)$/);
        if (request.method === "GET" && search) {
          const kept = runs.get(search[1])?.searches?.[search[2]];
          if (!kept) return send(response, 404, { message: `no results kept for search ${search[2]}` });
          return send(response, 200, kept);
        }

        const match = path.match(/^\/v1\/runs\/([^/]+)(\/events)?$/);
        if (request.method === "GET" && match) {
          const entry = runs.get(match[1]);
          if (!entry) return send(response, 404, { message: `no run ${match[1]}` });
          return match[2] ? follow(entry, request, response) : send(response, 200, view(entry));
        }

        if (request.method === "POST" && path === "/v1/clarify") {
          return send(response, 200, { questions: QUESTIONS });
        }
        if (request.method === "POST" && path === "/v1/runs") {
          const body = await read(request);
          // The service records a run under its method's name.
          const kind = String(body.method);
          // The recorded one, so a run started here has call content to show.
          const fixture = runs.get(kind === "storm" ? "article" : "wal")!;
          const id = Math.random().toString(16).slice(2, 14);
          const topic = String((body.input as { question?: string } | undefined)?.question ?? "");
          const entry = { ...replay(fixture.run, id, false, { kind, topic }), contents: fixture.contents };
          runs.set(id, entry);
          const frame = stream(response);
          frame("run", { run_id: id });
          // Held open until the replay ends, as the real response holds the run slot.
          const timer = setInterval(() => {
            if (view(entry).ended_at === null) return;
            clearInterval(timer);
            response.end();
          }, TICK_MS);
          request.on("close", () => clearInterval(timer));
          return;
        }
        send(response, 404, { message: `mock has no ${request.method} ${path}` });
      });
    },
  };
}
