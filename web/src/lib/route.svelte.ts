// `#/` starts a run, `#/runs` lists them, `#/runs/<id>/<tab>` shows one. In the hash, so the
// service serves the one page for every route.
export type Route = { page: "new" } | { page: "runs" } | { page: "run"; id: string; tab: string | null };

function parse(hash: string): Route {
  const run = hash.match(/^#\/runs\/([^/]+)(?:\/([a-z]+))?$/);
  if (run) return { page: "run", id: run[1], tab: run[2] ?? null };
  // Links from before the pages were split.
  const old = hash.match(/^#run\/([^/]+)(\/result)?$/);
  if (old) return { page: "run", id: old[1], tab: old[2] ? "result" : null };
  return hash === "#/runs" ? { page: "runs" } : { page: "new" };
}

export const href = (to: Route) =>
  to.page === "new" ? "#/" : to.page === "runs" ? "#/runs" : `#/runs/${to.id}${to.tab ? `/${to.tab}` : ""}`;

export const route = $state({ current: parse(location.hash) });
addEventListener("hashchange", () => {
  route.current = parse(location.hash);
});

// `replace` for a change within a page, such as a tab, so Back leaves the page.
export function go(to: Route, replace = false) {
  history[replace ? "replaceState" : "pushState"](null, "", location.pathname + location.search + href(to));
  route.current = to;
}
