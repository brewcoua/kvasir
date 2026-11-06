<script lang="ts">
  import type { ModelUsage } from "../lib/api";
  import { count, duration } from "../lib/format";
  import Icon from "./Icon.svelte";

  let { usage }: { usage: ModelUsage } = $props();
</script>

<span title="duration"><Icon name="clock" />{usage.seconds ? duration(usage.seconds) : "—"}</span>
<span title="calls"><Icon name="chip" />{count(usage.calls)}</span>
<span title="tokens"><Icon name="tokens" />{count(usage.prompt_tokens + usage.completion_tokens)}</span>
<span title="cost"><Icon name="coin" />{usage.cost.toFixed(4)}</span>
