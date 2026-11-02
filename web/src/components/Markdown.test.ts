import { render } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import Markdown from "./Markdown.svelte";

const show = (source: string) => render(Markdown, { source }).container.querySelector(".markdown")!;

describe("Markdown", () => {
  it("shows HTML from the source as text", () => {
    const root = show('<script>alert(1)</script>\n\nA <img src=x onerror="alert(2)"> and `<b>x</b>`');
    expect(root.querySelector("script, img, b")).toBeNull();
    expect(root.textContent).toContain("<script>alert(1)</script>");
    expect(root.textContent).toContain('<img src=x onerror="alert(2)">');
    expect(root.querySelector("code")!.textContent).toBe("<b>x</b>");
  });

  it("links only to the web", () => {
    const root = show("[safe](https://example.org/a) and [unsafe](javascript:alert(1))");
    const links = root.querySelectorAll("a");
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe("https://example.org/a");
    expect(links[0].rel).toBe("noreferrer noopener");
    expect(root.textContent).toContain("unsafe");
  });

  it("turns source numbers into badges, but not link text", () => {
    const root = show("Cited [3][12], linked [4](https://example.org).");
    const badges = [...root.querySelectorAll(".cite")].map((badge) => badge.getAttribute("href"));
    expect(badges).toEqual(["#cite-3", "#cite-12"]);
    expect(root.querySelector('a[href="https://example.org"]')!.textContent).toBe("4");
  });

  it("turns the unverified marker into a badge, in paragraphs, lists, callouts and tables", () => {
    const root = show(
      "Prices rose [2]. [unverified]\n\n- Item [3]. [unverified]\n\n> [!NOTE]\n> Quoted. [unverified]\n\n| a |\n|---|\n| cell [unverified] |",
    );
    expect(root.querySelectorAll(".unverified")).toHaveLength(4);
    for (const badge of root.querySelectorAll(".unverified")) {
      expect(badge.textContent).toBe("unverified");
      expect(badge.getAttribute("title")).toContain("No model could judge");
      expect(badge.closest("a")).toBeNull();
    }
    expect(root.querySelectorAll(".cite")).toHaveLength(2);
    expect(root.querySelector("li .unverified")).not.toBeNull();
    expect(root.querySelector(".callout .unverified")).not.toBeNull();
    expect(root.querySelector("td .unverified")).not.toBeNull();
  });

  it("moves headings down one level, to h4 at most", () => {
    const root = show("## Section\n\n#### Deep");
    expect(root.querySelector("h3")!.textContent).toBe("Section");
    expect(root.querySelector("h4")!.textContent).toBe("Deep");
  });

  it("reads a line of bold text as a heading", () => {
    const root = show("**Findings**:\nThe text under it.");
    expect(root.querySelector("h4")!.textContent).toBe("Findings");
    expect(root.querySelector("p")!.textContent).toBe("The text under it.");
  });

  it("keeps bold inside a sentence as bold", () => {
    const root = show("A **bold** word.");
    expect(root.querySelector("h4")).toBeNull();
    expect(root.querySelector("p strong")!.textContent).toBe("bold");
  });

  it("wraps a table so it scrolls, with its alignment", () => {
    const root = show("| Option | Price |\n| --- | ---: |\n| A [1] | 3 |");
    const table = root.querySelector(".scroll > table")!;
    expect(table.querySelector("th:last-child")!.getAttribute("style")).toContain("right");
    expect(table.querySelector("td .cite")).not.toBeNull();
  });

  it("nests lists by indentation", () => {
    const root = show("1. one\n   - inner\n2. two\n\n3. three");
    expect(root.querySelector("ol > li > ul > li")!.textContent).toBe("inner");
    expect(root.querySelectorAll("ol > li")).toHaveLength(3);
  });

  it("shows a callout with its kind and title", () => {
    const root = show("> [!WARNING] Disputed\n> The dates **disagree** [2].\n\nAfter.");
    const callout = root.querySelector("aside.callout.warning")!;
    expect(callout.querySelector(".title")!.textContent).toBe("Disputed");
    expect(callout.querySelector("strong")!.textContent).toBe("disagree");
    expect(callout.querySelector(".cite")).not.toBeNull();
    expect(root.lastElementChild!.textContent).toBe("After.");
  });

  it("titles a callout by its kind when it has no title", () => {
    expect(show("> [!NOTE]\n> Text.").querySelector(".callout .title")!.textContent).toBe("Note");
  });

  it("anchors headings as GitHub does, and links the contents to them", () => {
    const root = show("- [Trade & war](#trade--war)\n\n## Trade & war\n\n## Trade & war");
    expect([...root.querySelectorAll("h3")].map((h) => h.id)).toEqual(["trade--war", "trade--war-1"]);
    expect(root.querySelector('a[href="#trade--war"]')).not.toBeNull();
  });
});
