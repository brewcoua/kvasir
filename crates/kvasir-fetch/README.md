# kvasir-fetch

Reading a page from the web: who may be asked and how often, what is kept, and what a page that
a plain request cannot read falls back to.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `lib.rs` | `Page`, `Engine`, `Options` and `FetchError`, and the re-exports. |
| `web.rs` | `Web`: the clients, redirects, robots.txt, the cache and the fallback to a browser. |
| `guard.rs` | Keeping this service's own network out of reach of any URL a page or a model wrote. |
| `polite.rs` | How often a host is asked, and how many requests the process has open. |
| `robots.rs` | What a robots.txt allows this crawler, by RFC 9309. |
| `store.rs` | `pages.db`, so a page read once is not downloaded again while it is fresh. |
