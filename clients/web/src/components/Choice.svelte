<script lang="ts">
  import { Select } from "bits-ui";

  // `onchange` is when one is picked, and the shown value follows `value`, so one its owner refuses does not stick.
  let {
    id,
    value,
    choices,
    disabled = false,
    onchange,
  }: { id?: string; value: string; choices: string[]; disabled?: boolean; onchange: (value: string) => void } = $props();
</script>

<Select.Root type="single" {disabled} bind:value={() => value, onchange} items={choices.map((choice) => ({ value: choice, label: choice }))}>
  <Select.Trigger {id} class="select">{value}</Select.Trigger>
  <Select.Portal>
    <Select.Content class="menu" sideOffset={4}>
      <Select.Viewport>
        {#each choices as choice (choice)}<Select.Item value={choice} label={choice} class="option">{choice}</Select.Item>{/each}
      </Select.Viewport>
    </Select.Content>
  </Select.Portal>
</Select.Root>
