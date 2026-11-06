import { fireEvent, render } from "@testing-library/svelte";
import { tick } from "svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { loaded, loading, wantIcons } from "../lib/favicons.svelte";
import SiteGraph, { labelVisible } from "./SiteGraph.svelte";

// The real one loads through `Image`, which jsdom never finishes; what it does is in favicons.test.ts.
vi.mock("../lib/favicons.svelte", async () => {
  const { SvelteSet } = await import("svelte/reactivity");
  return { iconUrl: (host: string) => `/icon/${host}`, loaded: new SvelteSet<string>(), loading: new SvelteSet<string>(), wantIcons: vi.fn() };
});

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
const radii = (container: HTMLElement) => [...container.querySelectorAll(".node circle:not(.ring)")].map((circle) => Number(circle.getAttribute("r")));

describe("SiteGraph", () => {
  it("draws a circle per site and a line per edge between known sites", () => {
    const { container } = render(SiteGraph, props);
    expect(container.querySelectorAll(".node")).toHaveLength(3);
    expect(container.querySelectorAll(".link")).toHaveLength(2);
  });

  it("sizes a site by its pages, then by the sites it shares a line with", async () => {
    const { container, rerender } = render(SiteGraph, props);
    const [a, b] = radii(container);
    expect(a).toBeGreaterThan(b);
    await rerender({ ...props, size: "connections" });
    const [a2, b2, c2] = radii(container);
    expect(b2).toBeGreaterThan(a2);
    expect(a2).toBe(c2);
  });

  it("counts a site linked both ways once, and not its count of links", async () => {
    const many = [
      { source: "a.org", target: "b.org", count: 900 },
      { source: "b.org", target: "a.org", count: 900 },
      { source: "c.org", target: "a.org", count: 1 },
      { source: "c.org", target: "b.org", count: 1 },
    ];
    const { container } = render(SiteGraph, { ...props, edges: many, size: "connections" });
    const [a, b, c] = radii(container);
    expect(a).toBe(b);
    expect(a).toBe(c);
  });

  it("fades all but the hovered site, its neighbours and their lines", async () => {
    const { container } = render(SiteGraph, { ...props, edges: edges.slice(0, 1).concat({ source: "c.org", target: "d.org", count: 1 }), sites: [...sites, { domain: "d.org", pages: 1, links: 0 }] });
    const nodes = [...container.querySelectorAll(".node")];
    const classes = (name: string) => nodes.filter((node) => node.classList.contains(name)).map((node) => node.textContent);
    await fireEvent.pointerEnter(nodes[0]);
    expect(classes("lit")).toEqual(["a.org", "b.org"]);
    expect(classes("faded")).toEqual(["c.org", "d.org"]);
    expect([...container.querySelectorAll(".link")].map((link) => link.classList.contains("faded"))).toEqual([false, true]);
    await fireEvent.pointerLeave(nodes[0]);
    expect(container.querySelectorAll(".faded, .lit")).toHaveLength(0);
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

  it("outlines a site by its trust tier, and says its kind on hover", async () => {
    const source = (kind: string, tier: "high" | "low" | "avoid" | "normal") => ({ kind, tags: [], tier, label: "", qid: "", origin: "rule" });
    const sourced = [
      { ...sites[0], source: source("government", "high") },
      { ...sites[1], source: source("unknown", "avoid") },
      { ...sites[2], source: null },
    ];
    const { container } = render(SiteGraph, { ...props, sites: sourced });
    const nodes = [...container.querySelectorAll(".node")];
    expect(nodes.map((node) => [...node.classList].filter((name) => name.startsWith("tier-")))).toEqual([["tier-high"], ["tier-avoid"], []]);
    expect(nodes[1].querySelector("title")!.textContent).toBe("b.org: unknown, avoid");
    expect(nodes[2].querySelector("title")).toBeNull();
    await fireEvent.pointerEnter(nodes[1]);
    expect(container.querySelector(".site-card .source")!.textContent).toBe("unknown · avoid");
  });

  it("shades the biggest site apart from the smallest", () => {
    const { container } = render(SiteGraph, props);
    const shade = [...container.querySelectorAll<SVGGElement>(".node")].map((node) => node.style.getPropertyValue("--t"));
    expect(shade).toEqual(["1", "0.2", "0"]);
  });

  describe("favicons", () => {
    const icons = (container: HTMLElement) => [...container.querySelectorAll(".node image.favicon")];

    beforeEach(() => loaded.clear());

    it("asks for every site's icon, biggest first", () => {
      render(SiteGraph, props);
      expect(vi.mocked(wantIcons).mock.lastCall![0]).toEqual(["a.org", "b.org", "c.org"]);
    });

    it("draws an icon on any site, once it has loaded", async () => {
      const { container } = render(SiteGraph, props);
      expect(icons(container)).toHaveLength(0);
      loaded.add("c.org");
      await tick();
      const [icon, ...rest] = icons(container);
      expect(rest).toHaveLength(0);
      expect(icon.getAttribute("href")).toBe("/icon/c.org");
      expect(icon.closest(".node")!.textContent).toContain("c.org");
      expect(container.querySelectorAll(".node circle:not(.ring)")).toHaveLength(3);
    });

    it("marks only a site whose icon is being asked for, until it has it", async () => {
      const { container } = render(SiteGraph, props);
      const marked = () => [...container.querySelectorAll(".node .icon-loading")].map((ring) => ring.closest(".node")!.textContent);
      expect(marked()).toEqual([]);
      loading.add("b.org");
      await tick();
      expect(marked()).toHaveLength(1);
      expect(marked()[0]).toContain("b.org");
      loading.delete("b.org");
      loaded.add("b.org");
      await tick();
      expect(marked()).toEqual([]);
    });

    it("still picks a site when its icon is clicked", async () => {
      const onpick = vi.fn();
      loaded.add("a.org");
      const { container } = render(SiteGraph, { ...props, onpick });
      await fireEvent.click(icons(container)[0]);
      expect(onpick).toHaveBeenCalledWith("a.org");
    });
  });
});
