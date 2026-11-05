<script lang="ts">
  // The service sets the token as a cookie, which every later request, `EventSource` and `<img>`
  // included, sends by itself. Reloading starts the page over with it.
  let token = $state("");
  let error = $state("");
  let busy = $state(false);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    busy = true;
    error = "";
    try {
      const response = await fetch("/v1/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (response.ok) return location.reload();
      error = response.status === 401 ? "Wrong token." : `Login failed: ${response.status}`;
    } catch (failure) {
      error = String(failure);
    } finally {
      busy = false;
    }
  }
</script>

<form class="panel login" onsubmit={submit}>
  <h2>kvasir</h2>
  <label for="token">Token</label>
  <input id="token" type="password" autocomplete="current-password" bind:value={token} required />
  <p class="meta">The service's <code>KVASIR_TOKEN</code>.</p>
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  <button type="submit" class="action" disabled={busy || token === ""}>Log in</button>
</form>
