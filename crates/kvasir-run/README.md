# kvasir-run

What the service is doing, and what it cost.

A run is minutes of work behind a single streaming response, so a client that disconnects, or
one that never started the run, has no way to find out how it is going. The registry is that
record. It lives in memory; with `runs.store` set to sqlite each run is also written to a
[`RunStore`] when it starts and when it ends, which is history for debugging rather than state.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `run.rs` | One run, and everything observed about it while it works. |
| `registry.rs` | `RunRegistry`: the most recent runs. |
| `ctx.rs` | `RunCtx`: a line of work within a run, and what it reports into. |
| `slots.rs` | `Slots`: how many runs may go at once. |
| `usage.rs` | What a model, a role or a stage spent. |
| `store.rs` | `RunStore`, runs kept in SQLite, when `runs.store` is sqlite. |
