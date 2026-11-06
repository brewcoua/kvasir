<script lang="ts">
  import { request, type CacheStats, type PageDetail, type PageSummary, type Recall, type Site, type SiteGraph as Graph, type SourceInfo } from "../lib/api";
  import { count, duration, isWeb, size } from "../lib/format";
  import { go, href } from "../lib/route.svelte";
  import { Switch } from "bits-ui";
  import Favicon from "../components/Favicon.svelte";
  import Markdown from "../components/Markdown.svelte";
  import SiteGraph from "../components/SiteGraph.svelte";
  import SourceBadge from "../components/SourceBadge.svelte";
  import SourceMenu from "../components/SourceMenu.svelte";

  let { id }: { id: string | null } = $props();

  let sites = $state<Site[] | null>(null);
  let graph: Graph | null = $state(null);
  let stats: CacheStats | null = $state(null);
  let by: "pages" | "connections" = $state("pages");
  let site: string | null = $state(null);
  let pages: PageSummary[] = $state([]);
  let detail: PageDetail | null = $state(null);
  let recall: Recall | null = $state(null);
  let question = $state("");
  let error = $state("");
  let busy = $state(false);

  const when = (at: number) => new Date(at * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  const ago = (at: number) => `${duration(Math.max(Date.now() / 1000 - at, 0))} ago`;

  // Where the pieces fall under the recall minimum, in a list best first, is marked by a null.
  const rows = $derived.by(() => {
    if (!recall) return [];
    const cut = recall.pieces.findIndex((piece) => !piece.clears);
    const at = cut < 0 ? recall.pieces.length : cut;
    return [...recall.pieces.slice(0, at), null, ...recall.pieces.slice(at)];
  });

  async function attempt(work: () => Promise<void>) {
    busy = true;
    error = "";
    try {
      await work();
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    } finally {
      busy = false;
    }
  }

  async function load() {
    const [listed, drawn, held] = await Promise.all([
      request<Site[]>("GET", "/v1/sites"),
      request<Graph>("GET", "/v1/sites/graph"),
      request<CacheStats>("GET", "/v1/pages/stats"),
    ]);
    [sites, graph, stats] = [listed, drawn, held];
    if (site && !listed.some((one) => one.domain === site)) site = null;
    pages = site ? await request<PageSummary[]>("GET", `/v1/pages?site=${encodeURIComponent(site)}`) : [];
  }

  const chosen = $derived(sites?.find((one) => one.domain === site));

  function pick(domain: string | null) {
    site = domain;
    attempt(load);
  }

  function flush() {
    if (!confirm("Delete every cached page, link and piece? This cannot be undone.")) return;
    attempt(async () => {
      await request("DELETE", "/v1/pages");
      site = null;
      await load();
    });
  }

  async function ask() {
    recall = null;
    await attempt(async () => {
      recall = await request<Recall>("GET", `/v1/pages/recall?q=${encodeURIComponent(question)}`);
    });
  }

  function removeSite(domain: string) {
    if (!confirm(`Delete every cached page of ${domain}, with its pieces? This cannot be undone.`)) return;
    attempt(async () => {
      await request("DELETE", `/v1/sites/${encodeURIComponent(domain)}`);
      await load();
    });
  }

  // Only the marked site is read again, so the graph does not settle anew. The service answers 404 to
  // these when sources are off, and then no site has a `source` to mark.
  function mark(domain: string, value: string | null) {
    const path = `/v1/sources/${encodeURIComponent(domain)}`;
    attempt(async () => {
      await (value === null ? request("DELETE", `${path}/mark`) : request("PUT", `${path}/mark`, { value }));
      const source = await request<SourceInfo>("GET", path);
      const patch = <T extends { domain: string }>(list: T[]) => list.map((one) => (one.domain === domain ? { ...one, source } : one));
      if (sites) sites = patch(sites);
      if (graph) graph = { ...graph, sites: patch(graph.sites) };
    });
  }

  function removePage(page: PageDetail) {
    if (!confirm(`Delete this page and its pieces from the cache?\n${page.url}\nThis cannot be undone.`)) return;
    attempt(async () => {
      await request("DELETE", `/v1/pages/${page.id}`);
      go({ page: "knowledge", id: null });
    });
  }

  // The page may have moved since it was read, which caches it under another id.
  function refetch(page: PageDetail) {
    attempt(async () => {
      const next = await request<PageDetail>("POST", `/v1/pages/${page.id}/refetch`);
      if (next.id === page.id) detail = next;
      else go({ page: "knowledge", id: next.id });
    });
  }

  $effect(() => {
    detail = null;
    error = "";
    if (id) {
      attempt(async () => {
        detail = await request<PageDetail>("GET", `/v1/pages/${id}`);
      });
    } else attempt(load);
  });
</script>

{#snippet web(url: string)}
  {#if isWeb(url)}<a href={url} target="_blank" rel="noreferrer noopener">{url}</a>{:else}{url}{/if}
{/snippet}

{#if id}
  <section class="panel">
    <h2>Page <span class="meta"><a href={href({ page: "knowledge", id: null })}>Knowledge</a></span></h2>
    <div class="body">
      {#if detail}
        <p class="heading">{detail.title || detail.url}</p>
        <p class="meta"><Favicon url={detail.url} />{@render web(detail.url)}</p>
        <p class="meta">
          {when(detail.fetched_at)} · {ago(detail.fetched_at)} · {detail.engine}
          {#if detail.final_url !== detail.url}· served from {@render web(detail.final_url)}{/if}
        </p>
        <p class="actions">
          <button type="button" class="action" disabled={busy} onclick={() => refetch(detail!)}>Fetch again</button>
          <button type="button" class="action" disabled={busy} onclick={() => removePage(detail!)}>Delete</button>
        </p>
        <h3>Text</h3>
        <div class="extract">
          <Markdown source={detail.text} />
        </div>
        <h3>Leads</h3>
        {#each Object.entries(detail.leads) as [region, leads] (region)}
          <details>
            <summary>{region} ({leads.length})</summary>
            <ul class="citations">
              {#each leads as lead, i (i)}
                <li>{@render web(lead.url)}{#if lead.anchor}<span class="meta">{" · "}{lead.anchor}</span>{/if}</li>
              {/each}
            </ul>
          </details>
        {:else}
          <p class="meta">None.</p>
        {/each}
        <h3>Cited by</h3>
        {#if detail.runs.length > 0}
          <ul class="citations">
            {#each detail.runs as run (run.id)}
              <li><a href={href({ page: "run", id: run.id, tab: null })}>{run.topic}</a></li>
            {/each}
          </ul>
        {:else}
          <p class="meta">No stored run cites this page.</p>
        {/if}
      {:else if !error}
        <p class="meta">loading…</p>
      {/if}
      {#if error}<p class="error">{error}</p>{/if}
    </div>
  </section>
{:else}
  <div class="knowledge">
    <section class="panel graph-panel">
      <h2>Knowledge <span class="meta">what pages.db holds; the text shown is the text a run reads</span></h2>
      {#if graph && graph.sites.length === 0}
        <p class="empty">Nothing is cached yet. A run keeps each page it reads, and they show here.</p>
      {:else if graph}
        <div class="graph-fill">
          <SiteGraph sites={graph.sites} edges={graph.edges} size={by} selected={site} onpick={(domain) => pick(domain === site ? null : domain)} />
        </div>
        <div class="graph-bar">
          <span class="meta">Node size and shade = {by === "pages" ? "pages" : "connected sites"}; line width = links between sites.</span>
          <label class="switch-label">
            Size by {by}
            <Switch.Root class="switch" aria-label="Size by connections" checked={by === "connections"} onCheckedChange={(on) => (by = on ? "connections" : "pages")}>
              <Switch.Thumb class="switch-thumb" />
            </Switch.Root>
          </label>
        </div>
      {/if}
      {#if error}<p class="error body">{error}</p>{/if}
    </section>

    <div class="side-column">
      {#if stats}
        <section class="panel">
          <h2>Storage <span class="meta" title={stats.path}>{stats.path}</span></h2>
          <div class="body">
            <dl class="stats">
              <div><dt>pages</dt><dd>{count(stats.pages)}</dd></div>
              <div><dt>links</dt><dd>{count(stats.links)}</dd></div>
              <div><dt>pieces</dt><dd>{count(stats.pieces)}</dd></div>
              <div><dt>text</dt><dd>{size(stats.text_bytes)}</dd></div>
              <div><dt>embeddings</dt><dd>{size(stats.vector_bytes)}</dd></div>
              <div><dt>file on disk</dt><dd>{size(stats.file_bytes)}</dd></div>
              {#if stats.oldest !== null}<div><dt>oldest</dt><dd>{when(stats.oldest)}</dd></div>{/if}
              {#if stats.newest !== null}<div><dt>newest</dt><dd>{when(stats.newest)}</dd></div>{/if}
            </dl>
            {#if stats.pages > 0}
              <p class="actions"><button type="button" class="action danger" disabled={busy} onclick={flush}>Flush cache</button></p>
            {/if}
          </div>
        </section>
      {/if}

      {#if site}
        <section class="panel site-panel">
          <h2>
            <span class="topic"><Favicon url={`https://${site}`} />Pages of {site}</span>
            {#if chosen?.source}
              <SourceBadge source={chosen.source} />
              <SourceMenu domain={site} source={chosen.source} disabled={busy} onpick={(value) => mark(site!, value)} />
            {/if}
            <button type="button" class="action danger" disabled={busy} onclick={() => removeSite(site!)}>Delete site</button>
            <button type="button" class="action" onclick={() => pick(null)}>Close</button>
          </h2>
          {#if chosen}
            <p class="meta site-meta">{count(chosen.pieces)} pieces · last fetch {when(chosen.fetched_at)} · {chosen.engines.join(", ")}</p>
          {/if}
          <div class="body fill">
            <table>
              <thead>
                <tr><th>page</th><th class="n">pieces</th><th class="n">characters</th></tr>
              </thead>
              <tbody>
                {#each pages as page (page.id)}
                  <tr>
                    <td class="topic-cell">
                      <a href={href({ page: "knowledge", id: page.id })} class="topic">{page.title || page.url}</a>
                      <span class="meta">{page.engine} · {when(page.fetched_at)}</span>
                    </td>
                    <td class="n">{count(page.pieces)}</td>
                    <td class="n">{count(page.length)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        </section>
      {/if}

      <section class="panel recall-panel">
        <h2>Recall <span class="meta">the pieces a run would recall</span></h2>
        <div class="body">
          <form class="ask-recall" onsubmit={(event) => (event.preventDefault(), ask())}>
            <input type="text" bind:value={question} aria-label="Question" placeholder="A question, as a run would ask it" />
            <button type="submit" class="action" disabled={busy || question.trim() === ""}>Recall</button>
          </form>
          {#if recall}
            {#if recall.pieces.length === 0}
              <p class="meta">No cached piece matches the words of this question.</p>
            {:else}
              <div class="recall-results">
                <table>
                  <tbody>
                    {#each rows as piece, i (i)}
                      {#if piece}
                        <tr class:below={!piece.clears}>
                          <td class="n">{piece.similarity.toFixed(3)}</td>
                          <td class="piece">
                            <Favicon url={piece.url} />
                            <a href={href({ page: "knowledge", id: piece.page })} class="topic">{piece.title || piece.url}</a>
                            <span class="meta">{piece.site}</span>
                            {piece.text}
                          </td>
                        </tr>
                      {:else}
                        <tr class="cut"><td colspan="2">recall minimum {recall.minimum}: pieces above it are taken</td></tr>
                      {/if}
                    {/each}
                  </tbody>
                </table>
              </div>
            {/if}
          {/if}
        </div>
      </section>
    </div>
  </div>
{/if}
