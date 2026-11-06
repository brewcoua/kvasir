# kvasir-storm

STORM: an article written from simulated conversations between editors and an expert who
searches to answer them. Stanford OVAL's algorithm and prompts, MIT licensed; see `NOTICE`.

The stages are `research`, `outline`, `article` and, unless turned off, `polish`. Each editor
holds a conversation side by side with the others; the sections are written side by side too.
STORM reads search snippets only, not pages.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `lib.rs` | `run`, the method's entry point, through its four stages. |
| `curation.rs` | The research stage: the editors, and each one's conversation with the expert. |
| `write.rs` | The outline, the sections, and the polish. |
| `article.rs` | An article as a tree of sections, and the sources its `[n]` cite. |
| `text.rs` | Upstream's text clean-ups, quirks kept. |
| `signatures.rs` | STORM's signatures, upstream's prompts. |
| `prompts/` | The instructions of those signatures. |
