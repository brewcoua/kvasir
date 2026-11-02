// Markdown is only lexed here. The tokens are rendered by `Markdown.svelte`, which writes every
// piece as text, so nothing from a web page or a model is ever parsed as HTML.
import { Marked, type Token, type TokenizerExtension } from "marked";

export type { Token, Tokens } from "marked";

// The anchor `lex` gives each heading, as GitHub makes it.
declare module "marked" {
  namespace Tokens {
    interface Heading {
      id?: string;
    }
  }
}

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

// ` [unverified]`, which verification appends to a sentence no model could judge.
const unverified: TokenizerExtension = {
  name: "unverified",
  level: "inline",
  start: (source) => source.indexOf("[unverified]") >= 0 ? source.indexOf("[unverified]") : undefined,
  tokenizer(source) {
    const match = source.match(/^\[unverified\]/);
    if (match) return { type: "unverified", raw: match[0] };
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

// `> [!WARNING] Title`, then the quoted lines under it: a GitHub or Obsidian callout.
const callout: TokenizerExtension = {
  name: "callout",
  level: "block",
  start: (source) => source.match(/^ {0,3}> ?\[!/m)?.index,
  tokenizer(source) {
    const match = source.match(/^ {0,3}> ?\[!(\w+)\][+-]?[ \t]*([^\n]*)(?:\n|$)((?: {0,3}>[^\n]*(?:\n|$))*)/);
    if (match) {
      const body = match[3].replace(/^ {0,3}> ?/gm, "");
      return {
        type: "callout",
        raw: match[0],
        kind: match[1].toLowerCase(),
        title: match[2].trim() || match[1][0].toUpperCase() + match[1].slice(1).toLowerCase(),
        tokens: this.lexer.blockTokens(body, []),
      };
    }
  },
};

const marked = new Marked({ gfm: true }).use({ extensions: [cite, unverified, boldLine, callout] });

// A heading's anchor as GitHub makes it, so a report's contents link to its headings.
export function slug(text: string, taken: Map<string, number>): string {
  const base = text
    .toLowerCase()
    .trim()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-");
  const count = taken.get(base) ?? 0;
  taken.set(base, count + 1);
  return count ? `${base}-${count}` : base;
}

export function lex(source: string): Token[] {
  const tokens = marked.lexer(source.replace(/\r\n?/g, "\n"));
  const taken = new Map<string, number>();
  for (const token of tokens) if (token.type === "heading") token.id = slug(token.text, taken);
  return tokens;
}
