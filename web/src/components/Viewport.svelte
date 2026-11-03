<script lang="ts">
  import { getViewportForBounds, useStore, useSvelteFlow, type Rect } from "@xyflow/svelte";
  import { untrack } from "svelte";

  // A new object per request, so asking for the same box twice pans twice. `first` is the view on mount.
  let { focus, first }: { focus: { box: Rect } | null; first: Rect } = $props();

  const store = useStore();
  const { setViewport, getViewport } = useSvelteFlow();

  // Once the pane has a size; fitView would fit a whole open column and leave the text tiny.
  let placed = false;
  $effect(() => {
    if (placed || !store.width) return;
    placed = true;
    untrack(() => setViewport(getViewportForBounds(first, store.width, store.height, store.minZoom, 1, 0.1)));
  });

  // A box that fits at the current zoom is centred; a bigger one, such as an open research column, keeps the
  // zoom and shows its top left, since fitting it would leave the text unreadable.
  const PAD = 24;
  $effect(() => {
    if (!focus) return;
    untrack(() => {
      const { box } = focus;
      const { zoom } = getViewport();
      const fits = box.width * zoom <= store.width - 2 * PAD && box.height * zoom <= store.height - 2 * PAD;
      const target = fits
        ? getViewportForBounds(box, store.width, store.height, store.minZoom, zoom, 0.2)
        : { x: PAD - box.x * zoom, y: PAD - box.y * zoom, zoom };
      setViewport(target, { duration: 300 });
    });
  });
</script>
