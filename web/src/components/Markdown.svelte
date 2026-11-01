<script lang="ts">
  import { lex } from "../lib/markdown";
  import Blocks from "./Blocks.svelte";

  let { source }: { source: string } = $props();
  const tokens = $derived(lex(source));
</script>

<div class="markdown"><Blocks {tokens} /></div>
