<script lang="ts">
  import { DropdownMenu } from "bits-ui";
  import type { SourceInfo } from "../lib/api";
  import { KIND_LABELS } from "../lib/format";

  // `onpick` gets `allow`, `deny`, `exempt`, a kind, or null to clear the mark.
  let { domain, source, disabled = false, onpick }: { domain: string; source: SourceInfo; disabled?: boolean; onpick: (value: string | null) => void } = $props();

  const kinds = Object.keys(KIND_LABELS).filter((kind) => kind !== "unknown");
  // An exempt site keeps what the rest of the order found, so its origin is not `mark`.
  const marked = $derived(source.tags.includes("exempt") ? "exempt" : source.origin === "mark" ? (source.tags.find((tag) => tag === "allow" || tag === "deny") ?? source.kind) : "");
</script>

{#if source.origin === "config"}
  <button type="button" class="action" disabled title="In sources.allow or sources.deny, so it cannot be marked here">Locked</button>
{:else}
  <DropdownMenu.Root>
    <DropdownMenu.Trigger class="action" aria-label={`Mark ${domain}`} {disabled}>Mark</DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content class="menu" align="end" sideOffset={4}>
        <DropdownMenu.RadioGroup aria-label="Mark as" value={marked} onValueChange={(value) => onpick(value)}>
          {#each ["allow", "deny", "exempt", ...kinds] as value (value)}
            <DropdownMenu.RadioItem {value} class="option">{value}</DropdownMenu.RadioItem>
          {/each}
        </DropdownMenu.RadioGroup>
        <DropdownMenu.Item class="option" disabled={marked === ""} onSelect={() => onpick(null)}>Clear the mark</DropdownMenu.Item>
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>
{/if}
