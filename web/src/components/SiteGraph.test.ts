import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import SiteGraph, { labelVisible } from "./SiteGraph.svelte";

// jsdom has none, and `bind:clientWidth` needs one.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

const sites = [
  { domain: "a.org", pages: 100, links: 1 },
  { domain: "b.org", pages: 4, links: 50 },
  { domain: "c.org", pages: 0, links: 0 },
];
const edges = [
  { source: "a.org", target: "b.org", count: 3 },
  { source: "b.org", target: "c.org", count: 1 },
  { source: "b.org", target: "gone.org", count: 9 },
];
const props = { sites, edges, size: "pages" as const, selected: null, onpick: () => {} };
const radii = (container: HTMLElement) => [...container.querySelectorAll(".node circle")].map((circle) => Number(circle.getAttribute("r")));

describe("SiteGraph", () => {
  it("draws a circle per site and a line per edge between known sites", () => {
    const { container } = render(SiteGraph, props);
    expect(container.querySelectorAll(".node")).toHaveLength(3);
    expect(container.querySelectorAll(".link")).toHaveLength(2);
  });

  it("sizes a site by its pages, then by its links", async () => {
    const { container, rerender } = render(SiteGraph, props);
    const [a, b] = radii(container);
    expect(a).toBeGreaterThan(b);
    await rerender({ ...props, size: "links" });
    const [a2, b2] = radii(container);
    expect(b2).toBeGreaterThan(a2);
  });

  it("copes with nothing to size by", () => {
    const { container } = render(SiteGraph, { ...props, sites: [{ domain: "z.org", pages: 0, links: 0 }], edges: [] });
    expect(radii(container)).toEqual([6]);
  });

  it("picks a site on click or Enter, and marks the selected one", async () => {
    const onpick = vi.fn();
    const { container } = render(SiteGraph, { ...props, onpick, selected: "b.org" });
    const nodes = container.querySelectorAll(".node");
    await fireEvent.click(nodes[0]);
    await fireEvent.keyDown(nodes[2], { key: "Enter" });
    expect(onpick.mock.calls).toEqual([["a.org"], ["c.org"]]);
    expect(container.querySelectorAll(".node.selected")).toHaveLength(1);
  });

  it("labels a site once it is wide enough on screen, and always the selected one", () => {
    expect(labelVisible(6, 1)).toBe(false);
    expect(labelVisible(6, 3)).toBe(true);
    expect(labelVisible(34, 0.3)).toBe(false);
    const { container } = render(SiteGraph, { ...props, selected: "c.org" });
    const labelled = [...container.querySelectorAll(".node")].map((node) => node.classList.contains("labelled"));
    expect(labelled).toEqual([true, false, false]);
    expect(container.querySelector(".node.selected")!.textContent).toContain("c.org");
  });

  it("shows a card with the numbers while a site is hovered or focused", async () => {
    const { container } = render(SiteGraph, props);
    const nodes = container.querySelectorAll(".node");
    expect(container.querySelector(".site-card")).toBeNull();
    await fireEvent.pointerEnter(nodes[1]);
    const card = container.querySelector(".site-card")!;
    expect(card.textContent).toContain("b.org");
    expect(card.textContent).toContain("4 pages");
    expect(card.textContent).toContain("50 links");
    expect(card.textContent).toContain("2 connected sites");
    await fireEvent.pointerLeave(nodes[1]);
    expect(container.querySelector(".site-card")).toBeNull();
    await fireEvent.focus(nodes[0]);
    expect(container.querySelector(".site-card")!.textContent).toContain("1 connected sites");
    await fireEvent.blur(nodes[0]);
    expect(container.querySelector(".site-card")).toBeNull();
  });

  it("shows no card while a site is being dragged", async () => {
    const { container } = render(SiteGraph, props);
    const node = container.querySelectorAll(".node")[0];
    const view = container.ownerDocument.defaultView!;
    await fireEvent.pointerEnter(node);
    expect(container.querySelector(".site-card")).not.toBeNull();
    // d3-drag reads `event.view` to listen for the move and the release.
    const mouse = (type: string) => {
      const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0 });
      Object.defineProperty(event, "view", { value: view });
      return event;
    };
    await fireEvent(node, mouse("mousedown"));
    await fireEvent(view, mouse("mousemove"));
    expect(container.querySelector(".site-card")).toBeNull();
    await fireEvent(view, mouse("mouseup"));
    expect(container.querySelector(".site-card")).not.toBeNull();
  });

  it("shades the biggest site apart from the smallest", () => {
    const { container } = render(SiteGraph, props);
    const shade = [...container.querySelectorAll<SVGGElement>(".node")].map((node) => node.style.getPropertyValue("--t"));
    expect(shade).toEqual(["1", "0.2", "0"]);
  });
});
