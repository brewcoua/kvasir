# kvasir-ledger

The question ledger: a brief split into questions, searched for one at a time.

Code decides how much to search and when to stop; the model only decides what to search for,
what a page says, and how to write it up. Every model call sees one small input, so a 16k
context is enough. See `docs/research.md` for the design.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `lib.rs` | `research`, the method's entry point, from brief to verified report. |
| `brief.rs` | What the brief reads, and choosing what a ranking or a table compares. |
| `questions.rs` | The ledger: the questions to settle, and how far each got. |
| `search_loop.rs` | The question loop: one question searched at a time, each judged on what arrives. |
| `frontier.rs` | Which question to search next, with `frontier` set to `worth`. |
| `compare.rs` | Filling a ranking or a table, then ranking. |
| `repair.rs` | Searching again for what verification removed, and rewriting what the search supports. |
| `signatures.rs` | The signatures only this method uses. |
| `prompts/` | The instructions of those signatures. |
