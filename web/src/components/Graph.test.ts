import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import type { ResearchClaim, ResearchQuestion, RunDetail } from "../lib/api";
import Graph from "./Graph.svelte";

// jsdom has none, and `bind:clientWidth` needs one.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

const question = (id: number, parent: number | null, depth: number, claims: number[] = []): ResearchQuestion => ({
  id,
  text: `question ${id}`,
  parent,
  depth,
  status: claims.length > 0 ? "answered" : "open",
  claims,
  queries: [],
  missing: "",
});

const claim = (id: number, corroborated_by: number[] = []): ResearchClaim => ({
  id,
  text: `claim ${id}`,
  quote: "q",
  url: "https://example.org",
  title: "t",
  confidence: "high",
  step: 1,
  corroborated_by,
  contradicted_by: [],
});

// Only what the graph reads.
const live = (questions: ResearchQuestion[]) => ({ topic: "the topic", questions, result: null }) as unknown as RunDetail;
const done = (questions: ResearchQuestion[], claims: ResearchClaim[]) =>
  ({ topic: "the topic", questions, result: { report: "", claims, questions } }) as unknown as RunDetail;

describe("Graph", () => {
  it("hangs every question from the one that raised it, and first questions from the topic", () => {
    const { container } = render(Graph, {
      run: live([question(1, null, 0), question(2, null, 0), question(3, 1, 1), question(4, 3, 2)]),
    });
    expect(container.querySelectorAll(".node.topic")).toHaveLength(1);
    expect(container.querySelectorAll(".node.question")).toHaveLength(4);
    expect(container.querySelectorAll(".link.raised")).toHaveLength(4);
  });

  it("shows only the claims a question rests on, and the links between them", () => {
    const { container } = render(Graph, {
      run: done([question(1, null, 0, [1, 2])], [claim(1, [2]), claim(2, [1]), claim(3)]),
    });
    expect(container.querySelectorAll(".node.claim")).toHaveLength(2);
    expect(container.querySelectorAll(".link.rests")).toHaveLength(2);
    // Each claim names the other, which is one link.
    expect(container.querySelectorAll(".link.corroborates")).toHaveLength(1);
  });

  it("names a live question's claims until their text arrives with the result", async () => {
    const { container, getByText } = render(Graph, { run: live([question(1, null, 0, [5, 6])]) });
    await fireEvent.click(container.querySelector(".node.question")!);
    expect(getByText("Claims 5, 6. Their text shows once the run is done.")).toBeTruthy();
  });
});
