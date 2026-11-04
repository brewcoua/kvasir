<script lang="ts">
  import { request, type PageDetail, type PageSummary, type Recall, type Site } from "../lib/api";
  import { count, duration, isWeb } from "../lib/format";
  import { go, href } from "../lib/route.svelte";
  import Markdown from "../components/Markdown.svelte";

  let { id }: { id: string | null } = $props();

  let sites: Site[] | null = $state(null);
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
    sites = await request<Site[]>("GET", "/v1/sites");
    if (site && !sites.some((one) => one.domain === site)) site = null;
    pages = site ? await request<PageSummary[]>("GET", `/v1/pages?site=${encodeURIComponent(site)}`) : [];
  }

  function pick(domain: string | null) {
    site = domain;
    attempt(load);
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
        <p class="meta">{@render web(detail.url)}</p>
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
    <section class="panel">
      <h2>Knowledge <span class="meta">what pages.db holds; the text shown is the text a run reads</span></h2>
      {#if sites && sites.length === 0}
        <p class="empty">Nothing is cached yet. A run keeps each page it reads, and they show here.</p>
      {:else if sites}
        <div class="body scroll">
          <table>
            <thead>
              <tr><th>site</th><th class="n">pages</th><th class="n">pieces</th><th>last fetch</th><th>engines</th><th></th></tr>
            </thead>
            <tbody>
              {#each sites as one (one.domain)}
                <tr>
                  <td><button type="button" class="action" aria-pressed={site === one.domain} onclick={() => pick(one.domain)}>{one.domain}</button></td>
                  <td class="n">{count(one.pages)}</td>
                  <td class="n">{count(one.pieces)}</td>
                  <td class="meta">{when(one.fetched_at)}</td>
                  <td>{one.engines.join(", ")}</td>
                  <td class="n"><button type="button" class="action" disabled={busy} onclick={() => removeSite(one.domain)}>Delete</button></td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
      {#if error}<p class="error body">{error}</p>{/if}
    </section>

    {#if site}
      <section class="panel">
        <h2>Pages of {site} <button type="button" class="action" onclick={() => pick(null)}>Close</button></h2>
        <div class="body scroll">
          <table>
            <thead>
              <tr><th>page</th><th>engine</th><th>fetched</th><th class="n">pieces</th><th class="n">characters</th></tr>
            </thead>
            <tbody>
              {#each pages as page (page.id)}
                <tr>
                  <td class="topic-cell"><a href={href({ page: "knowledge", id: page.id })} class="topic">{page.title || page.url}</a></td>
                  <td>{page.engine}</td>
                  <td class="meta">{when(page.fetched_at)}</td>
                  <td class="n">{count(page.pieces)}</td>
                  <td class="n">{count(page.length)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/if}

    <section class="panel">
      <h2>Recall <span class="meta">the pieces a run would recall for a question</span></h2>
      <div class="body">
        <form class="ask-recall" onsubmit={(event) => (event.preventDefault(), ask())}>
          <input type="text" bind:value={question} aria-label="Question" placeholder="A question, as a run would ask it" />
          <button type="submit" class="action" disabled={busy || question.trim() === ""}>Recall</button>
        </form>
        {#if recall}
          {#if recall.pieces.length === 0}
            <p class="meta">No cached piece matches the words of this question.</p>
          {:else}
            <div class="scroll">
              <table>
                <thead><tr><th class="n">similarity</th><th>site</th><th>page</th><th>piece</th></tr></thead>
                <tbody>
                  {#each rows as piece, i (i)}
                    {#if piece}
                      <tr class:below={!piece.clears}>
                        <td class="n">{piece.similarity.toFixed(3)}</td>
                        <td>{piece.site}</td>
                        <td class="topic-cell"><a href={href({ page: "knowledge", id: piece.page })} class="topic">{piece.title || piece.url}</a></td>
                        <td class="piece">{piece.text}</td>
                      </tr>
                    {:else}
                      <tr class="cut"><td colspan="4">recall minimum {recall.minimum}: pieces above it are taken</td></tr>
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
{/if}
