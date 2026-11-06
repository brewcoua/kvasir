# kvasir

The kvasir binary.

- `kvasir serve` runs the service, configured by `KVASIR_*` variables and `kvasir.db`.
- `kvasir eval <out>` researches `eval/questions.toml` through a running service and scores
  each result; see `eval.rs`.
- `kvasir openapi` prints the OpenAPI document the web client's types are generated from.
- `kvasir pdf-text` reads a PDF on stdin and writes its text; the service runs it as a child.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `main.rs` | The subcommands, logging setup, and the `pdf-text` child. |
| `eval.rs` | `kvasir eval`: the questions in `eval/questions.toml`, run through a live service and scored. |
| `prompts/` | The instructions of the eval's checking call. |
