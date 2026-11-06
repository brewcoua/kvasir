# kvasir-browser

A page as a browser builds it, for pages whose text a script writes.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `lib.rs` | `Render`, the trait a renderer implements, and its error. |
| `chromium.rs` | Chromium over the DevTools protocol, behind the `chromium` feature. |
