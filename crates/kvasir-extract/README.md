# kvasir-extract

What a page says: its article as markdown, its links, and whether it holds an article at all.
Nothing here touches the network.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `html.rs` | HTML to markdown, and the page's links. |
| `check.rs` | Whether static HTML holds an article, or a challenge, a block page or an empty shell. |
| `split.rs` | A page cut into pieces one model call can read. |
| `normalize.rs` | The one spelling two URLs of a page share. |
| `pdf.rs` | The text layer of a PDF, read in a child process. |
