import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import type { ResearchQuestion, RunDetail } from "../lib/api";
import Graph from "./Graph.svelte";

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

// Only what the graph reads.
const live = (questions: ResearchQuestion[]) => ({ questions, result: null }) as unknown as RunDetail;

describe("Graph", () => {
  it("puts each question under the one that raised it", () => {
    const { container } = render(Graph, {
      run: live([question(1, null, 0), question(2, null, 0), question(3, 1, 1), question(4, 3, 2)]),
    });
    const texts = [...container.querySelectorAll(".node .text")].map((node) => node.textContent);
    expect(texts).toEqual(["question 1", "question 3", "question 4", "question 2"]);
    // One line per question that has a parent.
    expect(container.querySelectorAll("path")).toHaveLength(2);
  });

  it("names a live question's claims until their text arrives with the result", async () => {
    const { container, getByText } = render(Graph, { run: live([question(1, null, 0, [5, 6])]) });
    await fireEvent.click(container.querySelector(".node")!);
    expect(getByText("Claims 5, 6. Their text shows once the run is done.")).toBeTruthy();
  });
});
