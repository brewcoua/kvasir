<script lang="ts">
  import { onMount } from "svelte";
  import { defaultsOf, json, type ChallengeInfo, type Defaults, type MethodInfo, type RunSummary, type SettingsView } from "./lib/api";
  import { go, route } from "./lib/route.svelte";
  import Challenges from "./components/Challenges.svelte";
  import Icon from "./components/Icon.svelte";
  import Login from "./components/Login.svelte";
  import RunList from "./components/RunList.svelte";
  import Settings from "./components/Settings.svelte";
  import Setup from "./components/Setup.svelte";
  import Theme from "./components/Theme.svelte";
  import Knowledge from "./pages/Knowledge.svelte";
  import NewRun from "./pages/NewRun.svelte";
  import Run from "./pages/Run.svelte";
  import Runs from "./pages/Runs.svelte";

  const LIST_INTERVAL_MS = 3000;

  let runs: RunSummary[] = $state([]);
  let challenges: ChallengeInfo[] = $state([]);
  let summary = $state("loading…");
  let settingsOpen = $state(false);
  let defaults: Partial<Defaults> = $state({});
  // Null until the service says whether this browser holds the token.
  let authed: boolean | null = $state(null);
  // The stored settings while a required one is unset, which nothing can run without.
  let setup: SettingsView | null = $state(null);

  const active = $derived(runs.filter((run) => run.state === "running").length);

  async function refresh() {
    try {
      runs = await json<RunSummary[]>("/v1/runs");
      summary = `${runs.length} run(s), ${active} in flight`;
    } catch (error) {
      summary = String(error);
    }
  }

  function refreshChallenges() {
    json<ChallengeInfo[]>("/v1/search/challenges").then(
      (found) => (challenges = found),
      (error) => console.warn("no challenges:", error),
    );
  }

  // Again after the setup, since a model's default is empty until it is set.
  function loadDefaults() {
    json<MethodInfo[]>("/v1/methods")
      .then((methods) => (defaults = Object.assign({}, ...methods.map(defaultsOf))))
      .catch((error) => console.warn("no defaults:", error));
  }

  // `/` starts a new run from any page, unless it is being typed somewhere.
  function slash(event: KeyboardEvent) {
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target as HTMLElement;
    if (target.closest("input, textarea, select, [contenteditable]")) return;
    event.preventDefault();
    if (route.current.page === "new") document.querySelector<HTMLTextAreaElement>(".ask")?.focus();
    else go({ page: "new" });
  }

  onMount(() => {
    let timer: ReturnType<typeof setInterval> | undefined;
    // A service that cannot be reached is shown as such by the page itself, not as a login.
    fetch("/v1/settings").then(async (probe) => {
      if (probe.ok) {
        const view: SettingsView | null = await probe.json().catch(() => null);
        if (view?.missing.length) setup = view;
      }
      return probe.status !== 401;
    }, () => true).then((ok) => {
      authed = ok;
      if (!authed) return;
      // Rewrites a link from before the pages were split into its current form.
      go(route.current, true);
      refresh();
      loadDefaults();
      refreshChallenges();
      timer = setInterval(() => (refresh(), refreshChallenges()), LIST_INTERVAL_MS);
    });
    return () => clearInterval(timer);
  });
</script>

{#if authed === false}
  <Login />
{:else if setup}
  <Setup view={setup} ondone={() => ((setup = null), loadDefaults())} />
{:else if authed}
  <header>
    <h1><a href="#/">kvasir</a></h1>
    <nav>
      <a href="#/" aria-current={route.current.page === "new" ? "page" : undefined}>New</a>
      <a href="#/runs" aria-current={route.current.page === "runs" || route.current.page === "run" ? "page" : undefined}>
        Runs{#if active > 0}<span class="meta">{" · "}{active} running</span>{/if}
      </a>
      <a href="#/knowledge" aria-current={route.current.page === "knowledge" ? "page" : undefined}>Knowledge</a>
    </nav>
    <Theme />
    <button type="button" class="icon" title="Settings" aria-label="Settings" onclick={() => (settingsOpen = true)}>
      <Icon name="sliders" />
    </button>
  </header>

  <Challenges {challenges} onchange={refreshChallenges} />

  <main>
    {#if route.current.page === "run"}
      <div class="run-split">
        <section class="panel">
          <h2>Runs</h2>
          <RunList {runs} selected={route.current.id} />
        </section>
        {#key route.current.id}<Run id={route.current.id} tab={route.current.tab} />{/key}
      </div>
    {:else if route.current.page === "runs"}
      <Runs {runs} {summary} />
    {:else if route.current.page === "knowledge"}
      <Knowledge id={route.current.id} />
    {:else}
      <NewRun {defaults} {runs} onstarted={(id) => (go({ page: "run", id, tab: null }), refresh())} />
    {/if}
  </main>

  <Settings bind:open={settingsOpen} {defaults} />
{/if}

<svelte:window onkeydown={slash} />
