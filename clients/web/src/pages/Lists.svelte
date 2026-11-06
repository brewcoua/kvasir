<script lang="ts">
  import { Switch } from "bits-ui";
  import { request, type ListChange, type SourceExplanation, type SourceListInfo } from "../lib/api";
  import SourceBadge from "../components/SourceBadge.svelte";

  const EFFECTS = ["block", "demote", "exempt", "trust"];
  const lines = (text: string) => text.split(/[\s,]+/).filter(Boolean);
  const day = (at: number) => new Date(at * 1000).toLocaleDateString(undefined, { dateStyle: "medium" });

  let lists = $state<SourceListInfo[] | null>(null);
  let off = $state(false);
  let error = $state("");
  let busy = $state(false);
  let open = $state<string | null>(null);
  let host = $state("");
  let explained = $state<SourceExplanation | null>(null);
  let draft = $state({ name: "", url: "", entries: "", effect: "block", tag: "" });

  const groups = $derived.by(() => {
    const out = new Map<string, SourceListInfo[]>();
    for (const list of lists ?? []) out.set(list.group, [...(out.get(list.group) ?? []), list]);
    return [...out];
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

  // The service answers 404 to the whole of it when sources are off.
  async function load() {
    try {
      lists = await request<SourceListInfo[]>("GET", "/v1/sources/lists");
    } catch (reason) {
      if (!(reason instanceof Error) || !reason.message.includes("sources are off")) throw reason;
      off = true;
    }
  }
  attempt(load);

  const path = (id: string) => `/v1/sources/lists/${encodeURIComponent(id)}`;

  function change(id: string, fields: ListChange) {
    attempt(async () => {
      await request("PATCH", path(id), fields);
      await load();
    });
  }

  function setGroup(group: SourceListInfo[], enabled: boolean) {
    attempt(async () => {
      await Promise.all(group.filter((list) => list.enabled !== enabled).map((list) => request("PATCH", path(list.id), { enabled })));
      await load();
    });
  }

  function refresh(id: string) {
    attempt(async () => {
      await request("POST", `${path(id)}/refresh`);
      await load();
    });
  }

  function remove(list: SourceListInfo) {
    const question = list.custom ? `Delete the list ${list.name}? This cannot be undone.` : `Put ${list.name} back as it ships, off?`;
    if (!confirm(question)) return;
    attempt(async () => {
      await request("DELETE", path(list.id));
      await load();
    });
  }

  // A remote list of this effect lets whoever maintains it promote or unblock any site.
  const risky = $derived(draft.url.trim() !== "" && (draft.effect === "trust" || draft.effect === "exempt"));

  function add(event: SubmitEvent) {
    event.preventDefault();
    if (risky && !confirm(`Whoever maintains ${draft.url.trim()} could ${draft.effect === "trust" ? "make any site trusted" : "unblock any site"}. Add it?`)) return;
    const url = draft.url.trim();
    const fields: ListChange = { name: draft.name.trim(), effect: draft.effect, tag: draft.tag.trim(), ...(url ? { url } : { entries: lines(draft.entries) }) };
    attempt(async () => {
      await request("POST", "/v1/sources/lists", fields);
      draft = { name: "", url: "", entries: "", effect: "block", tag: "" };
      await load();
    });
  }

  function test(event: SubmitEvent) {
    event.preventDefault();
    explained = null;
    attempt(async () => {
      explained = await request<SourceExplanation>("GET", `/v1/sources/${encodeURIComponent(host.trim())}/explain`);
    });
  }

  const STATES: Record<string, string> = { applies: "applies", off: "is off", except: "excepts this site", guard: "spared by the guard" };
</script>

<section class="panel">
  <h2>Lists <span class="meta">which sites a run reads, ranks lower or never reads</span></h2>
  <div class="body lists">
    {#if off}
      <p class="empty">Source trust is off. Set <code>sources.enabled</code> to use lists.</p>
    {:else if !lists}
      <p class="meta">loading…</p>
    {:else}
      <form class="list-form" onsubmit={test}>
        <h3>Try a site</h3>
        <div class="list-fields">
          <input class="field" aria-label="Site to try" placeholder="example.org" bind:value={host} />
          <button type="submit" class="action" disabled={busy || !host.trim()}>Explain</button>
        </div>
        {#if explained}
          <p class="meta"><SourceBadge source={explained.source} link={false} /></p>
          {#if explained.lists.length}
            <ul class="citations">
              {#each explained.lists as hit (hit.id)}
                <li>{hit.name} <span class="meta">· {hit.effect} · {STATES[hit.state] ?? hit.state}</span></li>
              {/each}
            </ul>
          {:else}
            <p class="meta">No list names this site.</p>
          {/if}
        {/if}
      </form>

      {#each groups as [group, members] (group)}
        {@const config = group === "Config"}
        <h3>
          {group}
          {#if config}
            <span class="meta">set in sources.lists, so they change there</span>
          {:else}
            <button type="button" class="action" disabled={busy} onclick={() => setGroup(members, true)}>All on</button>
            <button type="button" class="action" disabled={busy} onclick={() => setGroup(members, false)}>All off</button>
          {/if}
        </h3>
        <ul class="list-rows">
          {#each members as list (list.id)}
            <li>
              <div class="list-row">
                {#if !config}
                  <Switch.Root class="switch" aria-label={`Use ${list.name}`} disabled={busy} checked={list.enabled} onCheckedChange={(enabled) => change(list.id, { enabled })}>
                    <Switch.Thumb class="switch-thumb" />
                  </Switch.Root>
                {/if}
                <span class="list-name">{list.name}</span>
                <span class="meta">
                  {list.effect}{#if list.tag}{" · "}{list.tag}{/if}
                  {#if list.hosts}{" · "}{list.hosts} sites{/if}
                  {#if list.skipped}{" · "}{list.skipped} rules unused{/if}
                  {#if list.fetched_at}{" · "}fetched {day(list.fetched_at)}{/if}
                  {#if list.licence}{" · "}licence: {list.licence}{/if}
                </span>
                {#if !config}
                  <button type="button" class="action" aria-expanded={open === list.id} onclick={() => (open = open === list.id ? null : list.id)}>Edit</button>
                {/if}
              </div>
              {#if list.error}<p class="error">Last download failed: {list.error}. Any older copy stays in use.</p>{/if}
              {#if open === list.id}
                <div class="list-edit">
                  <label>Effect
                    <select class="field" disabled={busy} value={list.effect} onchange={(event) => change(list.id, { effect: event.currentTarget.value })}>
                      {#each EFFECTS as effect (effect)}<option value={effect}>{effect}</option>{/each}
                      {#if !EFFECTS.includes(list.effect)}<option value={list.effect}>{list.effect}</option>{/if}
                    </select>
                  </label>
                  <label>Tag
                    <input class="field" disabled={busy} value={list.tag} onchange={(event) => change(list.id, { tag: event.currentTarget.value.trim() })} />
                  </label>
                  <label>Days between downloads
                    <input class="field" type="number" min="0" placeholder="sources.ttl_days" disabled={busy || !list.url} value={list.ttl_days} onchange={(event) => change(list.id, { ttl_days: Number(event.currentTarget.value) })} />
                  </label>
                  <label class="switch-label">Spare popular and suffix-rule sites
                    <Switch.Root class="switch" aria-label="Guard" disabled={busy} checked={list.guard} onCheckedChange={(guard) => change(list.id, { guard })}>
                      <Switch.Thumb class="switch-thumb" />
                    </Switch.Root>
                  </label>
                  <label>Sites this list does not apply to
                    <textarea class="field" disabled={busy} value={list.except.join("\n")} onchange={(event) => change(list.id, { except: lines(event.currentTarget.value) })}></textarea>
                  </label>
                  {#if list.url}
                    <p class="meta">{list.url}</p>
                  {:else}
                    <label>Sites
                      <textarea class="field" disabled={busy} value={list.entries.join("\n")} onchange={(event) => change(list.id, { entries: lines(event.currentTarget.value) })}></textarea>
                    </label>
                  {/if}
                  <p class="actions">
                    {#if list.url}<button type="button" class="action" disabled={busy} onclick={() => refresh(list.id)}>Fetch now</button>{/if}
                    <button type="button" class="action" disabled={busy} onclick={() => remove(list)}>{list.custom ? "Delete" : "Reset"}</button>
                  </p>
                </div>
              {/if}
            </li>
          {/each}
        </ul>
      {/each}

      <form class="list-form" onsubmit={add}>
        <h3>Add a list</h3>
        <div class="list-edit">
          <label>Name <input class="field" bind:value={draft.name} /></label>
          <label>URL <input class="field" placeholder="https://…" bind:value={draft.url} /></label>
          {#if !draft.url.trim()}
            <label>Or sites <textarea class="field" bind:value={draft.entries}></textarea></label>
          {/if}
          <label>Effect
            <select class="field" bind:value={draft.effect}>
              {#each EFFECTS as effect (effect)}<option value={effect}>{effect}</option>{/each}
            </select>
          </label>
          <label>Tag <input class="field" bind:value={draft.tag} /></label>
          {#if risky}<p class="error">Whoever maintains this list could {draft.effect === "trust" ? "make any site trusted" : "unblock any site"}.</p>{/if}
          <p class="actions"><button type="submit" class="action" disabled={busy || !draft.name.trim() || (!draft.url.trim() && !draft.entries.trim())}>Add</button></p>
        </div>
      </form>
    {/if}
    {#if error}<p class="error">{error}</p>{/if}
  </div>
</section>
