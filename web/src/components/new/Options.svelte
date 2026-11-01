<script lang="ts">
  import { DropdownMenu } from "bits-ui";
  import type { Launch } from "../../lib/launch.svelte";
  import Icon from "../Icon.svelte";

  let { launch }: { launch: Launch } = $props();

  const badges = $derived(
    launch.method.options.flatMap((option) => {
      const choice = option.choices.find((c) => c.value === launch.picked[option.name]);
      return choice ? [{ option, choice }] : [];
    }),
  );
</script>

{#if launch.method.options.length > 0}
  <DropdownMenu.Root>
    <DropdownMenu.Trigger class="icon add" title="Add an option" aria-label="Add an option">
      <Icon name="plus" />
    </DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content class="menu options" align="start" sideOffset={4}>
        {#each launch.method.options as option (option.name)}
          <DropdownMenu.RadioGroup
            value={launch.picked[option.name] ?? ""}
            onValueChange={(value) => (launch.picked[option.name] = value)}
          >
            <DropdownMenu.GroupHeading class="meta group">{option.label}</DropdownMenu.GroupHeading>
            {#each option.choices as choice (choice.value)}
              <DropdownMenu.RadioItem value={choice.value} class="option">
                <Icon name={choice.icon} />{choice.label}<span class="meta">{choice.hint}</span>
              </DropdownMenu.RadioItem>
            {/each}
          </DropdownMenu.RadioGroup>
        {/each}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>

  {#each badges as { option, choice } (option.name)}
    <span class="badge" title={`${option.label} ${choice.label.toLowerCase()}`}>
      <Icon name={choice.icon} />{choice.label}
      <button type="button" aria-label={`Remove ${choice.label}`} onclick={() => delete launch.picked[option.name]}>
        <Icon name="close" />
      </button>
    </span>
  {/each}
{/if}
