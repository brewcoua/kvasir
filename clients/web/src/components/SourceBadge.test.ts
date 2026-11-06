import { render } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import SourceBadge from "./SourceBadge.svelte";

const source = { kind: "government", tags: [], tier: "high" as const, label: "", qid: "", origin: "rule" };

describe("SourceBadge", () => {
  it("renders nothing without a source", () => {
    expect(render(SourceBadge, { source: null }).container.querySelector(".source")).toBeNull();
    expect(render(SourceBadge, { source: undefined }).container.querySelector(".source")).toBeNull();
  });

  it("shows a short kind and the tier", () => {
    const { container } = render(SourceBadge, { source });
    const badge = container.querySelector(".source")!;
    expect(badge.textContent).toBe("gov · high");
    expect(badge.classList.contains("tier-high")).toBe(true);
    expect(badge.tagName).toBe("SPAN");
  });

  it("leaves out an unknown tier", () => {
    const { container } = render(SourceBadge, { source: { ...source, kind: "unknown", tier: "unknown", origin: "" } });
    expect(container.querySelector(".source")!.textContent).toBe("unknown");
  });

  it("marks a site that is never read, and one of low trust", () => {
    const avoid = render(SourceBadge, { source: { ...source, kind: "unknown", tags: ["ai_generated"], tier: "avoid", origin: "list" } });
    expect(avoid.container.querySelector(".source")!.classList.contains("tier-avoid")).toBe(true);
    expect(avoid.container.querySelector(".source")!.textContent).toBe("unknown · avoid");
    const low = render(SourceBadge, { source: { ...source, kind: "news", tier: "low" as const } });
    expect(low.container.querySelector(".source")!.classList.contains("tier-low")).toBe(true);
  });

  it("links only a well-formed item, and not when asked not to", () => {
    const wiki = { ...source, qid: "Q52" };
    expect(render(SourceBadge, { source: { ...wiki, qid: "javascript:alert(1)" } }).container.querySelector("a")).toBeNull();
    expect(render(SourceBadge, { source: wiki, link: false }).container.querySelector("a")).toBeNull();
    expect(render(SourceBadge, { source: wiki }).container.querySelector("a")).not.toBeNull();
  });

  it("links to the Wikidata item when there is one", () => {
    const { container } = render(SourceBadge, { source: { ...source, kind: "reference", tier: "normal", label: "Wikipedia", qid: "Q52", origin: "wikidata" } });
    const link = container.querySelector("a.source")!;
    expect(link.getAttribute("href")).toBe("https://www.wikidata.org/wiki/Q52");
    expect(link.getAttribute("rel")).toBe("noreferrer noopener");
    expect(link.getAttribute("title")).toContain("Wikipedia");
  });
});
