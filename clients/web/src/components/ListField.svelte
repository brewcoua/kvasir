<script lang="ts">
  import { DropdownMenu } from "bits-ui";
  import { tick } from "svelte";
  import Icon from "./Icon.svelte";

  // An ordered list of strings, edited in place. Every edit goes through `onchange` and shows once `items` follows, so
  // one the owner refuses does not stick. With `choices`, entries come from those; without, they are typed or pasted.
  let {
    id,
    items,
    choices,
    placeholder = "Add an entry",
    disabled = false,
    onchange,
  }: { id: string; items: string[]; choices?: string[]; placeholder?: string; disabled?: boolean; onchange: (items: string[]) => void } = $props();

  let typed = $state("");
  let dragged = $state<number | null>(null);
  let over = $state<number | null>(null);
  let rows: HTMLOListElement | undefined = $state();

  const left = $derived((choices ?? []).filter((choice) => !items.includes(choice)));

  function add(text: string) {
    typed = "";
    const added = [...new Set(text.split(/[\s,]+/).filter(Boolean))].filter((item) => !items.includes(item));
    if (added.length > 0) onchange([...items, ...added]);
  }

  function move(from: number, to: number) {
    if (from === to) return;
    const next = [...items];
    next.splice(to, 0, ...next.splice(from, 1));
    onchange(next);
  }

  async function nudge(event: KeyboardEvent, at: number) {
    const to = at + (event.key === "ArrowUp" ? -1 : event.key === "ArrowDown" ? 1 : 0);
    if (to === at) return;
    event.preventDefault();
    if (to < 0 || to >= items.length) return;
    move(at, to);
    await tick();
    rows?.querySelectorAll<HTMLElement>(".list-grip")[to]?.focus();
  }
</script>

<div class="list-field">
  {#if items.length > 0}
    <ol bind:this={rows}>
      {#each items as item, i}
        <li
          draggable={!disabled}
          class:dragged={dragged === i}
          class:above={over === i && dragged !== null && dragged > i}
          class:below={over === i && dragged !== null && dragged < i}
          ondragstart={(event) => {
            dragged = i;
            // Firefox starts no drag without data.
            event.dataTransfer?.setData("text/plain", item);
          }}
          ondragover={(event) => {
            // Anything else dragged over the page is no entry of this list.
            if (dragged === null) return;
            event.preventDefault();
            over = i;
          }}
          ondrop={(event) => {
            event.preventDefault();
            if (dragged !== null) move(dragged, i);
          }}
          ondragend={() => (dragged = over = null)}
        >
          <button type="button" class="list-grip" {disabled} aria-label={`Move ${item}, with the up and down arrows`} onkeydown={(event) => nudge(event, i)}><Icon name="grip" /></button>
          <span class="list-item">{item}</span>
          <button type="button" class="list-remove" {disabled} aria-label={`Remove ${item}`} onclick={() => onchange(items.filter((_, at) => at !== i))}><Icon name="close" /></button>
        </li>
      {/each}
    </ol>
  {/if}
  {#if !choices}
    <div class="list-add">
      <Icon name="plus" />
      <input
        {id}
        type="text"
        autocomplete="off"
        {placeholder}
        {disabled}
        bind:value={typed}
        onblur={() => add(typed)}
        onkeydown={(event) => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          add(typed);
        }}
        onpaste={(event) => {
          const pasted = event.clipboardData?.getData("text") ?? "";
          if (!/[\s,]/.test(pasted.trim())) return;
          event.preventDefault();
          add(pasted);
        }}
      />
    </div>
  {:else if left.length > 0}
    <DropdownMenu.Root>
      <DropdownMenu.Trigger {id} class="action list-pick" {disabled}><Icon name="plus" />Add</DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content class="menu" align="start" sideOffset={4}>
          {#each left as choice (choice)}
            <DropdownMenu.Item class="option" onSelect={() => add(choice)}>{choice}</DropdownMenu.Item>
          {/each}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  {/if}
</div>
