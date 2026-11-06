# kvasir-api

The HTTP service: runs, the page cache, sites, settings and providers, behind one token.

[`open`] builds the shared state from the environment's settings and those stored in
`kvasir.db`, as the Python service's lifespan did; [`router`] serves it. Every route but health,
the login and the page needs the token: see `auth`. The routes answer as the FastAPI service
did, error bodies included, so a client cannot tell them apart. `docs/rust-plan.md` lists where
they differ.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `lib.rs` | `open`, `router` and the shared state, `App`. |
| `auth.rs` | The shared token, as a bearer header or the login cookie. |
| `body.rs` | Request bodies checked as FastAPI checked them, with the same 422 bodies. |
| `methods.rs` | `Method`, the closed set of research methods, and each method's options and schema. |
| `runs.rs` | Starting, listing and following runs, as server-sent events. |
| `tasks.rs` | `/v1/tasks` and `/v1/clarify`: one fast-model call each, outside any run. |
| `pages.rs` | The page cache by site and page, and removing from it. |
| `sources.rs` | Source kinds, user marks and site icons. |
| `settings.rs` | The stored settings: what each is, its value, and changing it. |
| `providers.rs` | The providers in the environment and the models each serves. |
| `page.rs` | The runs page, built from `web/`. |
| `openapi.rs` | The OpenAPI document, for `kvasir openapi`. |
