<script lang="ts">
  import { useSvelteFlow } from "@xyflow/svelte";
  import { tick, untrack } from "svelte";

  // A new object per request, so asking for the same nodes twice pans twice.
  let { focus }: { focus: { ids: string[] } | null } = $props();

  const { fitView, getViewport } = useSvelteFlow();

  $effect(() => {
    if (!focus) return;
    const nodes = focus.ids.map((id) => ({ id }));
    // The new nodes have to be measured before they can be fitted.
    tick().then(() => fitView({ nodes, padding: 0.2, duration: 300, maxZoom: untrack(() => getViewport().zoom) }));
  });
</script>
