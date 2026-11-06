# kvasir-llm

Calling language models: signatures rendered and parsed as dspy did, and an OpenAI-compatible
gateway client that retries what a retry can fix and reports what each call spent.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `signature.rs` | A signature: a model call's instructions and its typed fields. |
| `macros.rs` | Declaring a signature in a few lines. |
| `adapter.rs` | Prompts and replies in dspy 3.3's formats. |
| `shape.rs` | How a field's type is described to the model and read back from its reply. |
| `gateway.rs` | `Gateway`, the OpenAI-compatible client every provider is reached through. |
| `roles.rs` | Which model tier and token budget serves each step. |
