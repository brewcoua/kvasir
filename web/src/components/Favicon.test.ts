import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import Favicon from "./Favicon.svelte";

const icon = (container: HTMLElement) => container.querySelector("img.favicon");

describe("Favicon", () => {
  it("asks the backend for the site without www, encoded", () => {
    const { container } = render(Favicon, { url: "https://www.example.org:8080/a/b" });
    expect(icon(container)?.getAttribute("src")).toBe("/v1/favicons/example.org");
  });

  it("renders nothing for a link that is not web", () => {
    const { container } = render(Favicon, { url: "javascript:alert(1)" });
    expect(icon(container)).toBeNull();
  });

  it("hides after an error, and shows again for another url", async () => {
    const { container, rerender } = render(Favicon, { url: "https://missing.example/" });
    await fireEvent.error(icon(container)!);
    expect(icon(container)).toBeNull();
    await rerender({ url: "https://other.example/" });
    expect(icon(container)?.getAttribute("src")).toBe("/v1/favicons/other.example");
  });
});
