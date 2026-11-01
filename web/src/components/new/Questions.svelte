<script lang="ts">
  import type { Launch } from "../../lib/launch.svelte";

  let { launch }: { launch: Launch } = $props();
</script>

{#if launch.asked}
  <section class="questions">
    <h3>Before starting</h3>
    <p class="meta">
      Answer what you can, or <button type="button" class="link" onclick={() => launch.start()}>skip the questions</button>.
    </p>
    <ol>
      {#each launch.asked as question, i (i)}
        <li>
          <label for={`answer-${i}`}>{question}</label>
          <textarea id={`answer-${i}`} rows="1" bind:value={launch.answers[i]}></textarea>
        </li>
      {/each}
    </ol>
  </section>
{/if}
