<script module lang="ts">
  // Drawn inline, because nothing loads from another origin.
  const PATHS = {
    page: "M4 1.5h5.5L13 5v9.5H4zM9.5 1.5V5H13M6 8h5M6 10.5h5M6 13h3",
    sliders: "M2 4h6M11 4h3M2 8h2M7 8h7M2 12h8M13 12h1",
    search: "M10.3 10.3 14 14",
    chart: "M3 13V9M8 13V3M13 13V6M1.5 14.5h13",
    chip: "M6 2v2M10 2v2M6 12v2M10 12v2M2 6h2M2 10h2M12 6h2M12 10h2",
    start: "M3 8h10M9 4l4 4-4 4",
    plus: "M8 3v10M3 8h10",
    close: "M4.5 4.5l7 7M11.5 4.5l-7 7",
    ranking: "M2 4h12M2 8h8.5M2 12h5",
    table: "M2 3h12v10H2zM2 6.5h12M2 9.8h12M6.5 3v10",
    brief: "M10.5 2.5l3 3L6 13H3v-3z",
    write: "M3 4h10M3 7h10M3 10h10M3 13h6",
    verify: "M8 1.5l5 2v4c0 3.2-2.2 5.6-5 7-2.8-1.4-5-3.8-5-7v-4zM5.8 8l1.6 1.6 3-3",
    question: "M6.3 6.3a1.8 1.8 0 1 1 2.5 1.7c-.5.2-.8.6-.8 1.1v.5M8 11.4v.1",
    event: "",
    split: "M2 8h4M6 8l4-4h4M6 8l4 4h4M10 8h4",
    check: "M3 8.5l3.2 3L13 4.5",
    clock: "M8 4.5V8l2.5 1.5",
    coin: "M9.8 5.8H7.2a1.1 1.1 0 0 0 0 2.2h1.6a1.1 1.1 0 0 1 0 2.2H6.2M8 4.5v1.3M8 10.2v1.3",
    tokens: "M6 2.5l-1 11M11 2.5l-1 11M3 6h10.5M2.5 10h10.5",
    stage: "M2.5 4h11v8h-11zM2.5 7h11",
    background: "M8 2l6 3-6 3-6-3zM2 8l6 3 6-3M2 11l6 3 6-3",
    assess: "M2 12a6 6 0 1 1 12 0M8 12l2.5-3.5",
    outline: "M3 4h10M5.5 7h7.5M5.5 10h7.5M3 13h10",
    globe: "M2 8h12M8 2c2 1.8 2.6 3.8 2.6 6S10 12.2 8 14c-2-1.8-2.6-3.8-2.6-6S6 3.8 8 2",
    book: "M8 4.2C6.8 3 5 2.6 2.5 2.6v10c2.5 0 4.3.4 5.5 1.6 1.2-1.2 3-1.6 5.5-1.6v-10C11 2.6 9.2 3 8 4.2zM8 4.2v10",
    play: "M4.5 2.5v11l9-5.5z",
    download: "M8 2v8M4.8 7 8 10.2 11.2 7M2.5 13.5h11",
    image: "M2.5 3h11v10h-11zM2.5 11l3-3 2.5 2.5L10.5 8l3 3",
    tag: "M2.5 2.5h5l6 6-5 5-6-6zM5.2 5.2h.01",
    chevron: "M4 6l4 4 4-4",
    polish: "M8 2c.5 3 1.5 4.5 5 6-3.5 1.5-4.5 3-5 6-.5-3-1.5-4.5-5-6 3.5-1.5 4.5-3 5-6z",
  };

  export type IconName = keyof typeof PATHS;
</script>

<script lang="ts">
  let { name }: { name: IconName } = $props();
</script>

<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
  <g fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round">
    <path d={PATHS[name]} />
    {#if name === "sliders"}
      <circle cx="9.5" cy="4" r="1.5" /><circle cx="5.5" cy="8" r="1.5" /><circle cx="11.5" cy="12" r="1.5" />
    {:else if name === "search"}
      <circle cx="7" cy="7" r="4.5" />
    {:else if name === "chip"}
      <rect x="4" y="4" width="8" height="8" rx="1" />
    {:else if name === "question" || name === "clock" || name === "coin" || name === "globe"}
      <circle cx="8" cy="8" r="6" />
    {:else if name === "event"}
      <circle cx="8" cy="8" r="2.2" />
    {/if}
  </g>
</svg>
