# kvasir-sites

What kind of site a host is, and its icon.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `sources.rs` | `Sources`: a host's kind and tags, resolved in order from config, marks, lists, rules, cache and Wikidata. |
| `sources/store.rs` | `sources.db`: Wikidata's answers, which expire, and the marks, which do not. |
| `sources/lists.rs` | The downloaded host lists, refreshed on a TTL. |
| `sources/rsp.rs` | Wikipedia's perennial sources list, as `rsp_*` tags. |
| `sources/wikidata.rs` | Batched Wikidata lookups, their backoff, and the SPARQL query. |
| `sources.toml` | The bundled list: hosts tagged without any download, each with why. |
| `favicons.rs` | Site icons, fetched on demand and kept in `favicons.db`. |
