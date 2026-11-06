# kvasir-config

kvasir's settings, from the environment and the `settings` table of `kvasir.db`.

Two sources, each setting in exactly one. The environment holds what a deployment fixes and
what is secret: the token, the data directory, the address, thread width, logging and the model
providers. Everything else is stored and edited through the API; a field with nothing stored has
its default.

An environment variable is `KVASIR_<KEY>` for a top-level field and `KVASIR_LOG_<KEY>` for
logging. A provider is `KVASIR_PROVIDER_<NAME>_API_BASE` and `KVASIR_PROVIDER_<NAME>_API_KEY`. A
stored field is keyed by its path, `<section>.<key>`, so `research.max_searches`.

A bad environment stops the start. A bad stored value is reported in `warnings` and its default
used instead, since the API that would fix it must still come up. `strict` fails instead, for
checking a change before it is stored. The models and the search URL have no default: until they
are stored the service runs, and [`Settings::missing`] names them.

The messages are the ones Python wrote, values quoted as Python's `repr` quotes them, so
`tests/golden/settings.json` can hold this crate to them.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `lib.rs` | `Settings`, its errors, `Provider` and `Model`, and resolving a model reference. |
| `sections.rs` | The stored sections, such as `ResearchSettings`, with their defaults. |
| `load.rs` | `Settings::from_env`, `Settings::with_stored` and `decode`, with the checks each value passes. |
| `fields.rs` | Every stored field: its key, how its value is checked, and what the settings page shows. |
| `store.rs` | The `settings` table of `kvasir.db`. |
| `repr.rs` | Values quoted as Python's `repr` quotes them, for the messages. |
