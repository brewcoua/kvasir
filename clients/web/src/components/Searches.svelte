<script lang="ts">
  import { json, type RunDetail, type SearchCall, type SearchContent } from "../lib/api";
  import { isWeb } from "../lib/format";
  import Clock from "./Clock.svelte";
  import Favicon from "./Favicon.svelte";
  import SourceBadge from "./SourceBadge.svelte";

  // `event` filters the list to the searches made after that event, as on the calls tab.
  let { run, event = $bindable(null) }: { run: RunDetail; event: number | null } = $props();

  let expanded: number | null = $state(null);
  // Fetched once per search, on first expand.
  const contents = new Map<string, Promise<SearchContent>>();

  function content(id: number) {
    const path = `/v1/runs/${run.id}/searches/${id}`;
    if (!contents.has(path)) contents.set(path, json<SearchContent>(path));
    return contents.get(path)!;
  }

  const filtered = $derived(event === null ? run.searches : run.searches.filter((search) => search.event === event));
  const searches = $derived(filtered.slice().reverse());
  const shownEvent = $derived(event === null ? undefined : run.events.find((e) => e.seq === event));

  const toggle = (search: SearchCall) => (expanded = expanded === search.id ? null : search.id);
  const offset = (at: number) => (run.started_at ? `+${(at - run.started_at).toFixed(0)}s` : "");
</script>

{#if event !== null}
  <p class="filter">
    <span>Searches after event {event}{#if shownEvent}: <span class="stage">{shownEvent.stage}</span> {shownEvent.detail}{/if}</span>
    <button type="button" class="action" onclick={() => (event = null)}>Show all</button>
  </p>
{/if}

{#if searches.length === 0}
  <p class="empty">No searches.</p>
{:else}
  <div class="scroll">
    <table class="searches">
      <thead>
        <tr><th class="n">#</th><th class="n">event</th><th>query</th><th>engine</th><th class="n">at</th><th class="n">time</th><th class="n">results</th></tr>
      </thead>
      <tbody>
        {#each searches as search (search.id)}
          {@const open = expanded === search.id}
          <tr class="call" class:open class:failed={search.failures.length > 0} title={search.failures.length > 0 ? "failed" : undefined} onclick={() => toggle(search)}>
            <td class="n"><button type="button" class="link" aria-expanded={open}>{search.id}</button></td>
            <td class="n">
              {#if search.event != null}
                <button type="button" class="link" title="Show the searches after this event" onclick={(e) => (e.stopPropagation(), (event = search.event ?? null))}>{search.event}</button>
              {/if}
            </td>
            <td>{search.query}</td>
            <td>{search.engine}</td>
            <td class="n">{offset(search.started_at)}</td>
            <td class="n"><Clock from={search.started_at} to={search.ended_at} /></td>
            <td class="n">{search.results ?? "n/a"}</td>
          </tr>
          {#if open}
            <tr class="content">
              <td colspan="7">
                {#each search.failures as failure, f (f)}<p class="error">{failure}</p>{/each}
                {#if !search.has_content}
                  <p class="meta">Results not kept. Turn on runs.keep_call_content to keep what searches return.</p>
                {:else if search.ended_at === null}
                  <p class="meta">Running. Its results show once it answers.</p>
                {:else}
                  {#await content(search.id)}
                    <p class="meta">loading…</p>
                  {:then kept}
                    {#if kept.results.length === 0}<p class="meta">No results.</p>{/if}
                    <ol>
                      {#each kept.results as result, r (r)}
                        <li>
                          {#if isWeb(result.url)}<Favicon url={result.url} /><a href={result.url} target="_blank" rel="noreferrer noopener">{result.title || result.url}</a>{:else}{result.title || result.url}{/if}
                          <SourceBadge source={result.source} />
                          <div class="meta">{result.url}</div>
                          {#if result.snippet}<div>{result.snippet}</div>{/if}
                        </li>
                      {/each}
                    </ol>
                  {:catch error}
                    <p class="error">{error}</p>
                  {/await}
                {/if}
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
  </div>
{/if}
