# kvasir-graph

The graph method: questions explored best first, each raising the next from its evidence.

The brief is split into first questions, as in the ledger. Exploration then searches the
question proof numbers pick, takes in its claims, and adds the questions they raise as its
children, until the research time is spent or no question can still be settled. The report has
sections an outline call groups the questions into after the research, each written from the
claims its questions rest on. Always a report: the graph has no ranking or table. See
`docs/research.md` for the design.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `lib.rs` | `research`, the method's entry point, and `outline`, which groups the questions into sections. |
| `graph.rs` | The graph: questions, the claims answering them, and the questions claims raise. |
| `explore.rs` | One step of exploration: search the picked question, take in its claims, add what they raise. |
| `proof.rs` | Proof-number search: the question that settles the most of the brief for the least work. |
| `signatures.rs` | The signatures only this method uses. |
| `prompts/` | The instructions of those signatures. |
