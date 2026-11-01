<script lang="ts">
  import { json, type Progress, type RunDetail, type RunStatus } from "../lib/api";
  import { go } from "../lib/route.svelte";
  import Detail from "../components/Detail.svelte";

  let { id, tab }: { id: string; tab: string | null } = $props();

  // The run as its last snapshot, status and events left it.
  let detail: RunDetail | null = $state(null);
  let error = $state("");

  function merged<T extends { id?: number | null }>(known: T[], recent: T[]): T[] {
    const byId = new Map(known.map((item) => [item.id, item]));
    for (const item of recent) byId.set(item.id, item);
    return [...byId.values()];
  }

  // Follows the run, including one this page did not start. The page is made again for another
  // run, so `detail` never goes back to null under a view still showing it.
  $effect(() => {
    const run = id;
    json<RunDetail>(`/v1/runs/${run}`).then(
      (answer) => (detail ??= answer),
      (reason) => (error = String(reason)),
    );
    const source = new EventSource(`/v1/runs/${run}/events`);
    source.addEventListener("snapshot", (message) => {
      detail = JSON.parse(message.data);
      // The server closes the stream once the run is over. EventSource would reconnect for ever
      // otherwise, since it treats any close as a connection to retry.
      if (detail?.ended_at !== null) source.close();
    });
    // It carries only the most recent calls and searches, so they are merged into those known.
    source.addEventListener("status", (message) => {
      const { calls, searches, ...status }: RunStatus = JSON.parse(message.data);
      if (detail?.id !== run) return;
      detail = { ...detail, ...status, calls: merged(detail.calls, calls), searches: merged(detail.searches, searches) };
    });
    source.addEventListener("progress", (message) => {
      const event: Progress = JSON.parse(message.data);
      if (detail?.id === run) detail.events.push(event);
    });
    return () => source.close();
  });
</script>

<section class="panel">
  <h2>Run</h2>
  <div class="body">
    {#if detail}
      <Detail
        run={detail}
        tab={detail.result ? (tab ?? "result") : tab === "result" || !tab ? "overview" : tab}
        ontab={(next) => go({ page: "run", id, tab: next }, true)}
      />
    {:else if error}
      <p class="error">{error}</p>
    {:else}
      <p class="meta">loading…</p>
    {/if}
  </div>
</section>
