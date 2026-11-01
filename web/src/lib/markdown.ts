// Markdown is only lexed here. The tokens are rendered by `Markdown.svelte`, which writes every
// piece as text, so nothing from a web page or a model is ever parsed as HTML.
import { Marked, type Token, type TokenizerExtension } from "marked";

export type { Token, Tokens } from "marked";

// `[3]`, a source number. Not `[3](url)`, which is a link.
const cite: TokenizerExtension = {
  name: "cite",
  level: "inline",
  start: (source) => source.match(/\[\d+\](?!\()/)?.index,
  tokenizer(source) {
    const match = source.match(/^\[(\d+)\](?!\()/);
    if (match) return { type: "cite", raw: match[0], number: Number(match[1]) };
  },
};

// A line that is nothing but bold text. Models write these as sub-headings.
const boldLine: TokenizerExtension = {
  name: "boldLine",
  level: "block",
  tokenizer(source) {
    const match = source.match(/^ {0,3}\*\*([^*\n]+)\*\*:?[ \t]*(?:\n+|$)/);
    if (match) {
      return { type: "boldLine", raw: match[0], tokens: this.lexer.inlineTokens(match[1].trim()) };
    }
  },
};

const marked = new Marked({ gfm: true }).use({ extensions: [cite, boldLine] });

export const lex = (source: string): Token[] => marked.lexer(source.replace(/\r\n?/g, "\n"));
