<script lang="ts">
  import { Collapsible } from "bits-ui";
  import type { Snippet } from "svelte";
  import Icon from "./Icon.svelte";

  let { open = $bindable(false), class: className = "", head, children }: { open?: boolean; class?: string; head: Snippet; children: Snippet } = $props();
</script>

<Collapsible.Root bind:open class={["disclosure", className]}>
  <Collapsible.Trigger class="disclosure-head"><Icon name="chevron" />{@render head()}</Collapsible.Trigger>
  <!-- Stays in the page while closed, so the browser's find still reaches it, and opens it. -->
  <Collapsible.Content hiddenUntilFound class="disclosure-body">{@render children()}</Collapsible.Content>
</Collapsible.Root>
