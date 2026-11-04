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
type PageDetail = components["schemas"]["PageDetail"];
type SourceInfo = components["schemas"]["SourceInfo"];

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const SPEED = Number(process.env.MOCK_SPEED ?? 20);
// How far into the ranking run `running` is frozen.
const FROZEN_AT = 0.6;
const PAUSE_SECONDS = 10;
const TICK_MS = 500;
// As many calls as the service puts in a `status` event.
const STATUS_CALLS = 50;

const MODELS = { model_fast: "local/gpt-oss:20b", model_strong: "local/gpt-oss:20b" };
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

// `loop` starts the replay again after a pause, for a run that is always in progress. It holds just
// short of its end through the pause: an ended snapshot makes the page close its stream, and it
// would never see the restart.
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
      const at = start + (loop ? Math.min(elapsed - offset, length - 0.001) : elapsed - offset) * SPEED;
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

// A 1x1 PNG, served as every host's icon but one.
const ICON = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPwqnkAAAK6AadmxdSEAAAAAElFTkSuQmCC",
  "base64",
);
// The host with no icon, for the page to fall back on.
const NO_ICON = "missing.example";

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
      // A series update arrives again under its first `seq`, with a new `updated_at`.
      const known = new Map(last.events.map((event) => [event.seq, event.updated_at]));
      const fresh = next.events.filter((event) => !known.has(event.seq) || known.get(event.seq) !== event.updated_at);
      for (const event of fresh.sort((a, b) => (a.updated_at ?? a.at ?? 0) - (b.updated_at ?? b.at ?? 0))) frame("progress", event);
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

const cached = (id: string, url: string, title: string): PageDetail => ({
  id,
  url,
  final_url: url,
  title,
  engine: "static",
  fetched_at: 1793760000,
  text: `# ${title}\n\nBody of ${title}.`,
  leads: {},
  runs: [],
});
// The page cache, as `/v1/sites` and `/v1/pages` answer: two pages of one site, and one each of four more.
const PAGES: PageDetail[] = [
  {
    id: "3f2a9c1d0b7e4a56",
    url: "https://en.wikipedia.org/wiki/Roman_Empire",
    final_url: "https://en.wikipedia.org/wiki/Roman_Empire",
    title: "Roman Empire",
    engine: "static",
    fetched_at: 1793790000,
    text: "# Roman Empire\n\nThe Roman Empire ruled the Mediterranean from 27 BC.\n\n## Government\n\nThe emperor held supreme power.",
    leads: {
      related: [{ url: "https://en.wikipedia.org/wiki/Byzantine_Empire", anchor: "Byzantine Empire", context: "See also: Byzantine Empire" }],
      main: [
        { url: "https://en.wikipedia.org/wiki/Augustus", anchor: "Augustus", context: "founded by Augustus in 27 BC" },
        { url: "https://www.britannica.com/place/Roman-Empire", anchor: "Britannica", context: "Britannica on the Roman Empire" },
      ],
    },
    runs: [{ id: "report", topic: "What led to the recent backlash on Proton?" }],
  },
  {
    id: "8b1e0c4d92aa7f13",
    url: "https://en.wikipedia.org/wiki/Augustus",
    final_url: "https://en.wikipedia.org/wiki/Augustus",
    title: "Augustus",
    engine: "render",
    fetched_at: 1793780000,
    text: "# Augustus\n\nAugustus was the first Roman emperor.",
    leads: {},
    runs: [],
  },
  {
    id: "c47d5e2f10b93a68",
    url: "https://www.britannica.com/place/Roman-Empire",
    final_url: "https://www.britannica.com/place/Roman-Empire",
    title: "Roman Empire | Britannica",
    engine: "static",
    fetched_at: 1793770000,
    text: "# Roman Empire\n\nThe Roman Empire was the post-republican state of ancient Rome.",
    leads: {},
    runs: [],
  },
  cached("a1c3e5f70912b4d6", "https://www.cdc.gov/flu/", "Influenza"),
  cached("b2d4f6a81023c5e7", "https://www.bbc.com/news", "BBC News"),
  cached("d4f6b8c03245e7a9", "https://explainx.ai/blog/post", "An explainer"),
];

// What `/v1/sources` answers for the hosts above: a government, a news and an avoid that the config
// sets, so the Knowledge page has one of each and a locked one. Any other host is unknown.
const source = (kind: string, tier: SourceInfo["tier"], origin: string, more: Partial<SourceInfo> = {}): SourceInfo => ({
  kind,
  tags: [],
  tier,
  label: "",
  qid: "",
  origin,
  ...more,
});
const SOURCES: Record<string, SourceInfo> = {
  "en.wikipedia.org": source("reference", "normal", "wikidata", { tags: ["community", "official"], label: "Wikipedia", qid: "Q52" }),
  "cdc.gov": source("government", "high", "rule", { tags: ["official"] }),
  "bbc.com": source("news", "normal", "wikidata", { tags: ["official"], label: "BBC" }),
  "explainx.ai": source("unknown", "avoid", "config", { tags: ["deny"] }),
};
// The values a mark takes and the tiers they give repeat research/sources.py (MARKS, tier()); keep them in step.
const MARKS = ["allow", "deny", "intergovernmental", "government", "academic", "standards", "news", "reference", "organization"];

export function mock(): Plugin {
  const runs = load();
  const pages = [...PAGES];
  const marks = new Map<string, string>();
  const describe = (host: string): SourceInfo => {
    const mark = marks.get(host);
    if (mark === "allow" || mark === "deny") return source("unknown", mark === "deny" ? "avoid" : "high", "mark", { tags: [mark] });
    if (mark) return source(mark, ["intergovernmental", "government", "academic", "standards"].includes(mark) ? "high" : "normal", "mark");
    return SOURCES[host] ?? source("unknown", "unknown", "");
  };
  const site = (url: string) => new URL(url).hostname.replace(/^www\./, "");
  const leads = (page: PageDetail) => Object.values(page.leads).flat().map((lead) => lead.url);
  const listed = (page: PageDetail) => ({
    id: page.id,
    url: page.url,
    title: page.title,
    engine: page.engine,
    fetched_at: page.fetched_at,
    pieces: page.text.split("\n\n").length,
    length: page.text.length,
  });
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

        const icon = path.match(/^\/v1\/favicons\/([^/]+)$/);
        if (request.method === "GET" && icon) {
          if (decodeURIComponent(icon[1]) === NO_ICON) {
            response.statusCode = 404;
            return response.end();
          }
          response.setHeader("Content-Type", "image/png");
          return response.end(ICON);
        }
        if (request.method === "DELETE" && path === "/v1/favicons") {
          response.statusCode = 204;
          return response.end();
        }

        const sourced = path.match(/^\/v1\/sources\/([^/]+)(\/mark)?$/);
        if (sourced) {
          const host = decodeURIComponent(sourced[1]);
          if (request.method === "GET" && !sourced[2]) return send(response, 200, describe(host));
          if (sourced[2] && (request.method === "PUT" || request.method === "DELETE")) {
            if (SOURCES[host]?.origin === "config") return send(response, 409, { message: `${host} is set in the config, so a mark cannot change it` });
            if (request.method === "DELETE") {
              marks.delete(host);
              response.statusCode = 204;
              return response.end();
            }
            const value = String((await read(request)).value);
            if (!MARKS.includes(value)) return send(response, 422, { message: `cannot mark ${host} as ${value}` });
            marks.set(host, value);
            return send(response, 200, describe(host));
          }
        }
        if (request.method === "DELETE" && path === "/v1/sources") {
          response.statusCode = 204;
          return response.end();
        }

        if (request.method === "GET" && path === "/v1/sites") {
          const sites = [...new Set(pages.map((page) => site(page.url)))].map((domain) => {
            const held = pages.filter((page) => site(page.url) === domain);
            return {
              domain,
              pages: held.length,
              pieces: held.reduce((n, page) => n + listed(page).pieces, 0),
              fetched_at: Math.max(...held.map((page) => page.fetched_at)),
              engines: [...new Set(held.map((page) => page.engine))],
              source: describe(domain),
            };
          });
          return send(response, 200, sites);
        }
        if (request.method === "GET" && path === "/v1/sites/graph") {
          const domains = [...new Set(pages.map((page) => site(page.url)))];
          const sites = domains.map((domain) => {
            const held = pages.filter((page) => site(page.url) === domain);
            return { domain, pages: held.length, links: held.reduce((n, page) => n + leads(page).length, 0), source: describe(domain) };
          });
          const counts = new Map<string, number>();
          for (const page of pages)
            for (const target of leads(page)) {
              const to = site(target);
              if (to !== site(page.url) && domains.includes(to)) counts.set(`${site(page.url)} ${to}`, (counts.get(`${site(page.url)} ${to}`) ?? 0) + 1);
            }
          const edges = [...counts].map(([key, n]) => ({ source: key.split(" ")[0], target: key.split(" ")[1], count: n }));
          return send(response, 200, { sites, edges });
        }
        if (request.method === "GET" && path === "/v1/pages/stats") {
          const stamps = pages.map((page) => page.fetched_at);
          const text = pages.reduce((n, page) => n + page.text.length, 0);
          return send(response, 200, {
            pages: pages.length,
            links: pages.reduce((n, page) => n + leads(page).length, 0),
            pieces: pages.reduce((n, page) => n + listed(page).pieces, 0),
            text_bytes: text,
            vector_bytes: text * 12,
            file_bytes: pages.length ? 4096 * 300 : 0,
            path: "/data/pages.db",
            oldest: stamps.length ? Math.min(...stamps) : null,
            newest: stamps.length ? Math.max(...stamps) : null,
          });
        }
        if (request.method === "DELETE" && path === "/v1/pages") {
          pages.length = 0;
          response.statusCode = 204;
          return response.end();
        }
        if (request.method === "GET" && path === "/v1/pages") {
          const wanted = url.searchParams.get("site");
          return send(response, 200, pages.filter((page) => !wanted || site(page.url) === wanted).map(listed));
        }
        if (request.method === "GET" && path === "/v1/pages/recall") {
          const [page] = pages;
          const pieces = page ? [{ page: page.id, url: page.url, title: page.title, site: site(page.url), text: page.text, similarity: 0.71, clears: true }] : [];
          return send(response, 200, { minimum: 0.6, pieces });
        }
        const cached = path.match(/^\/v1\/(pages|sites)\/([^/]+)$/);
        if (cached) {
          const [, kind, key] = cached;
          const matches = (page: PageDetail) => (kind === "pages" ? page.id === key : site(page.url) === key);
          if (!pages.some(matches)) return send(response, 404, { message: `no ${kind === "pages" ? "page" : "pages of"} ${key}` });
          if (request.method === "GET" && kind === "pages") return send(response, 200, pages.find(matches));
          if (request.method === "DELETE") {
            pages.splice(0, pages.length, ...pages.filter((page) => !matches(page)));
            response.statusCode = 204;
            return response.end();
          }
        }

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
