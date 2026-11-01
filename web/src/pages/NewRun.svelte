<script lang="ts">
  import type { Defaults, RunSummary } from "../lib/api";
  import { duration } from "../lib/format";
  import { Launch } from "../lib/launch.svelte";
  import { values } from "../lib/methods";
  import Icon from "../components/Icon.svelte";
  import Methods from "../components/new/Methods.svelte";
  import Options from "../components/new/Options.svelte";
  import Plan from "../components/new/Plan.svelte";
  import Questions from "../components/new/Questions.svelte";
  import Recent from "../components/new/Recent.svelte";

  let { defaults, runs, onstarted }: { defaults: Partial<Defaults>; runs: RunSummary[]; onstarted: (id: string) => void } =
    $props();

  const launch = new Launch((id) => onstarted(id));

  const own = $derived(runs.filter((run) => run.kind === launch.method.run));
  const size = $derived(launch.method.size(values(launch.method, defaults), launch.picked));
  // How long finished runs of this method took, whatever their settings were.
  const took = $derived.by(() => {
    const times = own
      .filter((run) => run.state === "done" && run.started_at !== null && run.ended_at !== null)
      .map((run) => run.ended_at! - run.started_at!)
      .sort((a, b) => a - b);
    if (times.length === 0) return "";
    if (times.length === 1) return `The one finished run took ${duration(times[0])}.`;
    const middle = times[Math.floor(times.length / 2)];
    return `Finished runs took ${duration(times[0])} to ${duration(times.at(-1)!)}, ${duration(middle)} at the median.`;
  });

  // Enter starts, as the field is a question rather than a document. Shift+Enter breaks the line.
  function enter(event: KeyboardEvent & { currentTarget: HTMLTextAreaElement }) {
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }
</script>

<div class="new">
  <div class="compose">
    <form onsubmit={(e) => (e.preventDefault(), launch.submit())}>
      <Methods {launch} />
      <div class="box">
        <!-- svelte-ignore a11y_autofocus -->
        <textarea
          class="ask"
          rows="2"
          required
          autofocus
          bind:value={launch.text}
          oninput={launch.reset}
          onkeydown={enter}
          placeholder={launch.method.placeholder}
          aria-label="Question or topic"
        ></textarea>
        <div class="bar">
          <Options {launch} />
          <button type="submit" class="start" disabled={launch.busy || launch.text.trim() === ""} title="Start (Enter)" aria-label="Start">
            <Icon name="start" />
          </button>
        </div>
      </div>
      <p class="meta status">{launch.status || launch.method.blurb}</p>
      <Questions {launch} />
    </form>
    <Recent runs={own} label={launch.method.label.toLowerCase()} />
  </div>
  <aside class="side">
    <h3>Plan</h3>
    <p class="size">{size}</p>
    {#if took}<p class="meta took">{took}</p>{/if}
    <Plan method={launch.method} picked={launch.picked} {defaults} />
  </aside>
</div>
