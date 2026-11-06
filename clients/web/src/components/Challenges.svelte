<script lang="ts">
  import { Dialog } from "bits-ui";
  import type { ChallengeInfo } from "../lib/api";

  // The tab's own size on the service, which the page's pixels are counted in.
  const WIDTH = 1280;
  const HEIGHT = 720;
  const ENDINGS: Record<string, string> = {
    expired: "This challenge expired. The next search that is blocked opens another.",
    left: "The page left the provider's site, so the session was closed.",
    failed: "The session ended before the block was cleared.",
  };

  let { challenges, onchange }: { challenges: ChallengeInfo[]; onchange: () => void } = $props();

  let solving: string | null = $state(null);
  let frame = $state("");
  let ending = $state("");
  let socket: WebSocket | null = null;

  $effect(() => {
    if (solving === null) return;
    frame = "";
    ending = "";
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const opened = new WebSocket(`${scheme}://${location.host}/v1/search/challenges/${encodeURIComponent(solving)}/live`);
    socket = opened;
    let ended = false;
    opened.onmessage = (event) => {
      const message: { frame?: string; state?: string } = JSON.parse(event.data);
      if (message.frame) frame = message.frame;
      if (!message.state) return;
      ended = true;
      onchange();
      if (message.state === "cleared") solving = null;
      else ending = ENDINGS[message.state] ?? message.state;
    };
    opened.onclose = () => {
      if (!ended) ending = ENDINGS.failed;
    };
    return () => {
      opened.onclose = null;
      opened.close();
      socket = null;
    };
  });

  function send(input: Record<string, unknown>) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(input));
  }

  function mouse(event: MouseEvent, action: "move" | "press" | "release" | "wheel") {
    const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
    send({
      type: "mouse",
      action,
      x: ((event.clientX - box.left) * WIDTH) / box.width,
      y: ((event.clientY - box.top) * HEIGHT) / box.height,
      pressed: event.buttons === 1,
      delta_y: event instanceof WheelEvent ? event.deltaY : 0,
    });
  }

  function key(event: KeyboardEvent, down: boolean) {
    // Escape closes the dialog, and a paste arrives as its own event.
    if (event.key === "Escape") return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v") return;
    event.preventDefault();
    const text = event.key === "Enter" ? "\r" : event.key.length === 1 ? event.key : undefined;
    send({ type: "key", down, key: event.key, code: event.code, key_code: event.keyCode, text });
  }
</script>

{#if challenges.length > 0}
  <div class="challenges" role="status">
    {#each challenges as { provider } (provider)}
      <p>{provider} blocked a search and is resting until someone clears it.</p>
      <button type="button" class="action" onclick={() => (solving = provider)}>Solve {provider}</button>
    {/each}
  </div>
{/if}

<Dialog.Root bind:open={() => solving !== null, (open) => { if (!open) solving = null; }}>
  <Dialog.Portal>
    <Dialog.Overlay class="overlay" />
    <Dialog.Content class="settings challenge">
      <header>
        <Dialog.Title class="title">Clear {solving}'s block</Dialog.Title>
        <Dialog.Close class="action">Close</Dialog.Close>
      </header>
      <p class="meta">This is a browser on the service, seen from here. It closes when the block is gone.</p>
      {#if ending}
        <p class="error" role="alert">{ending}</p>
      {:else}
        <!-- The tab's own page: what it offers to assistive technology does not reach here. -->
        <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
        <div
          class="tab"
          tabindex="0"
          role="application"
          aria-label="{solving}'s page"
          onmousemove={(event) => mouse(event, "move")}
          onmousedown={(event) => (event.currentTarget.focus(), event.preventDefault(), mouse(event, "press"))}
          onmouseup={(event) => mouse(event, "release")}
          onwheel={(event) => (event.preventDefault(), mouse(event, "wheel"))}
          onkeydown={(event) => key(event, true)}
          onkeyup={(event) => key(event, false)}
          onpaste={(event) => send({ type: "text", text: event.clipboardData?.getData("text") ?? "" })}
        >
          {#if frame}<img alt="" draggable="false" src="data:image/jpeg;base64,{frame}" />{/if}
        </div>
      {/if}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
