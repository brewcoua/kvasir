// `#/` starts a run, `#/runs` lists them, `#/runs/<id>/<tab>` shows one, `#/knowledge` shows the page
// cache, `#/knowledge/<id>` one page of it and `#/lists` the source lists. In the hash, so the
// service serves the one page for every route.
export type Route =
  | { page: "new" }
  | { page: "runs" }
  | { page: "lists" }
  | { page: "run"; id: string; tab: string | null }
  | { page: "knowledge"; id: string | null };

function parse(hash: string): Route {
  const run = hash.match(/^#\/runs\/([^/]+)(?:\/([a-z]+))?$/);
  if (run) return { page: "run", id: run[1], tab: run[2] ?? null };
  // Links from before the pages were split.
  const old = hash.match(/^#run\/([^/]+)(\/result)?$/);
  if (old) return { page: "run", id: old[1], tab: old[2] ? "result" : null };
  const knowledge = hash.match(/^#\/knowledge(?:\/([0-9a-f]+))?$/);
  if (knowledge) return { page: "knowledge", id: knowledge[1] ?? null };
  if (hash === "#/lists") return { page: "lists" };
  return hash === "#/runs" ? { page: "runs" } : { page: "new" };
}

export const href = (to: Route) =>
  to.page === "new"
    ? "#/"
    : to.page === "runs"
      ? "#/runs"
      : to.page === "lists"
        ? "#/lists"
        : to.page === "knowledge"
          ? `#/knowledge${to.id ? `/${to.id}` : ""}`
          : `#/runs/${to.id}${to.tab ? `/${to.tab}` : ""}`;

export const route = $state({ current: parse(location.hash) });
addEventListener("hashchange", () => {
  route.current = parse(location.hash);
});

// `replace` for a change within a page, such as a tab, so Back leaves the page.
export function go(to: Route, replace = false) {
  history[replace ? "replaceState" : "pushState"](null, "", location.pathname + location.search + href(to));
  route.current = to;
}
