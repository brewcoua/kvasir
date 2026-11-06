<script lang="ts">
  import { AlertDialog } from "bits-ui";
  import { asking, reply } from "../lib/sure.svelte";
</script>

<AlertDialog.Root open={asking.question !== null} onOpenChange={(open) => open || reply(false)}>
  <AlertDialog.Portal>
    <AlertDialog.Overlay class="overlay" />
    <AlertDialog.Content class="confirm">
      <AlertDialog.Title class="title">Are you sure?</AlertDialog.Title>
      <AlertDialog.Description class="confirm-text">{asking.question?.text}</AlertDialog.Description>
      <div class="actions">
        <AlertDialog.Cancel class="action">Cancel</AlertDialog.Cancel>
        <AlertDialog.Action class="action primary" onclick={() => reply(true)}>{asking.question?.action}</AlertDialog.Action>
      </div>
    </AlertDialog.Content>
  </AlertDialog.Portal>
</AlertDialog.Root>
