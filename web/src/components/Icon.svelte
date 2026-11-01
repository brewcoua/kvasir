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
    {/if}
  </g>
</svg>
