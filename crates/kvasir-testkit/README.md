# kvasir-testkit

Stand-ins for the services kvasir calls, for tests: a scripted language model, and the golden
check of a signature.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `lib.rs` | `ScriptedLlm`: a model whose replies, by call or by role, and embeddings a test writes. |
| `golden.rs` | The check of a signature against `tests/golden/signatures.json`. |
