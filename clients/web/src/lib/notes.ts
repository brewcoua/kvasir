// What the one popover of the result shows, for a citation badge or a ranking cell.
import { getContext, setContext } from "svelte";
import { Tooltip } from "bits-ui";
import type { Citation, SourceInfo } from "./api";

export interface Note {
  title: string;
  meta?: string;
  // A page URL, for the icon beside the title.
  icon?: string;
  source?: SourceInfo | null;
  // Each quote, with where it was read.
  quotes?: { quote: string; meta?: string }[];
}

export type Tether = ReturnType<typeof Tooltip.createTether<Note>>;

interface Notes {
  tether: Tether;
  sources: () => Map<number, Citation>;
}

const KEY = Symbol("notes");
export const provideNotes = (notes: Notes) => setContext(KEY, notes);
export const useNotes = () => getContext<Notes>(KEY);
