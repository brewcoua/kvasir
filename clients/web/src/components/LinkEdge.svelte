<script lang="ts">
  import { BaseEdge, EdgeLabel, type EdgeProps } from "@xyflow/svelte";
  import type { LinkData } from "../lib/layout";

  let { id, data: raw }: EdgeProps = $props();

  const data = $derived(raw as LinkData);
</script>

{#if data.badge}
  <!-- Under the arc, in the container's colour, so the trunk stubs it crosses seem to pass beneath. -->
  <BaseEdge id="{id}-halo" path={data.path} class="halo" interactionWidth={0} />
{/if}
<BaseEdge {id} path={data.path} />
{#if data.badge}
  <EdgeLabel x={data.badge.x} y={data.badge.y} class="arc-badge {data.kind} {data.dim ? 'dim' : ''}">{data.count}</EdgeLabel>
{/if}
