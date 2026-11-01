<script lang="ts">
  import { json, type LmCall, type LmCallContent, type RunDetail } from "../lib/api";
  import { count, rate } from "../lib/format";
  import Clock from "./Clock.svelte";

  // `event` filters the list to the calls made after that event, in its line of work.
  let { run, event = $bindable(null) }: { run: RunDetail; event: number | null } = $props();

  let expanded: number | null = $state(null);
  // Fetched once per call, on first expand. Plain, not state, as the template fills it.
  const contents = new Map<string, Promise<LmCallContent>>();

  function content(id: number) {
    const path = `/v1/runs/${run.id}/calls/${id}`;
    if (!contents.has(path)) contents.set(path, json<LmCallContent>(path));
    return contents.get(path)!;
  }

  const filtered = $derived(event === null ? run.calls : run.calls.filter((call) => call.event === event));
  // Newest first, so a call in flight sits on top with its clock running.
  const calls = $derived(filtered.slice().reverse());
  const shownEvent = $derived(event === null ? undefined : run.events.find((e) => e.seq === event));

  const toggle = (call: LmCall) => (expanded = expanded === call.id ? null : (call.id ?? null));

  const offset = (at: number) => (run.started_at ? `+${(at - run.started_at).toFixed(0)}s` : "");
  const seconds = (call: LmCall) => (call.ended_at === null ? 0 : call.ended_at - call.started_at);
  // A message's content is text, or a list of parts when it carries more than text.
  const text = (content: unknown) => (typeof content === "string" ? content : JSON.stringify(content, null, 2));
</script>

{#if event !== null}
  <p class="filter">
    <span>Calls after event {event}{#if shownEvent}: <span class="stage">{shownEvent.stage}</span> {shownEvent.detail}{/if}</span>
    <button type="button" class="action" onclick={() => (event = null)}>Show all</button>
  </p>
{/if}

{#if calls.length === 0}
  <p class="empty">No model calls.</p>
{:else}
  <div class="scroll">
    <table class="calls">
      <thead>
        <tr>
          <th class="n">#</th><th class="n">event</th><th>role</th><th>model</th><th class="n">at</th><th class="n">time</th>
          <th class="n">in</th><th class="n">out</th><th class="n">tok/s</th><th></th>
        </tr>
      </thead>
      <tbody>
        {#each calls as call (call.id)}
          {@const open = call.id != null && expanded === call.id}
          <tr class="call" class:open onclick={() => toggle(call)}>
            <td class="n"><button type="button" class="link" aria-expanded={open}>{call.id}</button></td>
            <td class="n">
              {#if call.event != null}
                <button type="button" class="link" title="Show the calls after this event" onclick={(e) => (e.stopPropagation(), (event = call.event ?? null))}>{call.event}</button>
              {/if}
            </td>
            <td>{call.role ?? ""}</td>
            <td>{call.model}</td>
            <td class="n">{offset(call.started_at)}</td>
            <td class="n"><Clock from={call.started_at} to={call.ended_at} /></td>
            <td class="n">{call.prompt_tokens === null ? "" : count(call.prompt_tokens)}</td>
            <td class="n">{call.completion_tokens === null ? "" : count(call.completion_tokens)}</td>
            <td class="n">{rate(call.completion_tokens, seconds(call))}</td>
            <td>{call.ended_at === null ? "running" : call.failed ? "failed" : ""}</td>
          </tr>
          {#if open}
            <tr class="content">
              <td colspan="10">
                {#if !call.has_content || call.id == null}
                  <p class="meta">Content not kept. Set KVASIR_KEEP_CALL_CONTENT=true to keep what calls send and answer.</p>
                {:else if call.ended_at === null}
                  <p class="meta">Running. Its content shows once it answers.</p>
                {:else}
                  {#await content(call.id)}
                    <p class="meta">loading…</p>
                  {:then kept}
                    {#each kept.messages as message, m (m)}
                      <h4>{message.role}</h4>
                      <pre>{text(message.content)}</pre>
                    {/each}
                    {#if kept.reasoning}
                      <h4>reasoning</h4>
                      <pre>{kept.reasoning}</pre>
                    {/if}
                    <h4>response</h4>
                    <pre>{kept.response ?? "none"}</pre>
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
