import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import SourceMenu from "./SourceMenu.svelte";

const source = { kind: "unknown", tags: [], tier: "unknown" as const, label: "", qid: "", origin: "" };

describe("SourceMenu", () => {
  afterEach(cleanup);

  it("shows a site the config sets as locked, with no menu", () => {
    const { container } = render(SourceMenu, { source: { ...source, tags: ["deny"], tier: "avoid" as const, origin: "config" }, onpick: () => {}, domain: "x.org" });
    const button = container.querySelector("button")!;
    expect(button.textContent).toBe("Locked");
    expect(button.disabled).toBe(true);
  });

  it("offers allow, deny and the kinds, and reports the pick", async () => {
    const onpick = vi.fn();
    render(SourceMenu, { domain: "x.org", source, onpick });
    const trigger = screen.getByRole("button", { name: "Mark x.org" });
    await fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
    const items = (await screen.findAllByRole("menuitemradio")).map((item) => item.textContent);
    expect(screen.getByRole("menuitem", { name: "Clear the mark" }).getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByRole("group", { name: "Mark as" })).toBeTruthy();
    expect(items).toEqual(["allow", "deny", "intergovernmental", "government", "academic", "standards", "news", "reference", "organization"]);
    await fireEvent.click(screen.getByRole("menuitemradio", { name: "news" }));
    expect(onpick).toHaveBeenCalledWith("news");
  });

  it("clears a mark, and has nothing to clear without one", async () => {
    const onpick = vi.fn();
    const marked = { ...source, kind: "news", tier: "normal" as const, origin: "mark" };
    render(SourceMenu, { domain: "x.org", source: marked, onpick });
    await fireEvent.pointerDown(screen.getByRole("button", { name: "Mark x.org" }), { button: 0, ctrlKey: false });
    expect(await screen.findByRole("menuitemradio", { name: "news" })).toHaveProperty("ariaChecked", "true");
    expect(screen.getByRole("menuitem", { name: "Clear the mark" }).getAttribute("aria-disabled")).toBe("false");
    await fireEvent.click(screen.getByRole("menuitem", { name: "Clear the mark" }));
    expect(onpick).toHaveBeenCalledWith(null);
  });
});
