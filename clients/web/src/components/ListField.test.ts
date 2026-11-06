import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import ListField from "./ListField.svelte";

afterEach(cleanup);

function setup(items: string[], choices?: string[]) {
  const onchange = vi.fn();
  render(ListField, { props: { id: "list", items, choices, onchange } });
  return onchange;
}

describe("ListField", () => {
  it("adds what is typed, once, and clears the box", async () => {
    const onchange = setup(["a.example"]);
    const input = screen.getByRole<HTMLInputElement>("textbox");
    await fireEvent.input(input, { target: { value: "b.example" } });
    await fireEvent.keyDown(input, { key: "Enter" });
    expect(onchange).toHaveBeenCalledWith(["a.example", "b.example"]);
    expect(input.value).toBe("");

    onchange.mockClear();
    await fireEvent.input(input, { target: { value: "a.example" } });
    await fireEvent.blur(input);
    expect(onchange).not.toHaveBeenCalled();
  });

  it("takes a pasted list as separate entries", async () => {
    const onchange = setup([]);
    await fireEvent.paste(screen.getByRole("textbox"), { clipboardData: { getData: () => "a.example\nb.example, a.example" } });
    expect(onchange).toHaveBeenCalledWith(["a.example", "b.example"]);
  });

  it("removes an entry", async () => {
    const onchange = setup(["a", "b", "c"]);
    await fireEvent.click(screen.getByRole("button", { name: "Remove b" }));
    expect(onchange).toHaveBeenCalledWith(["a", "c"]);
  });

  it("moves an entry with the arrows, and not past either end", async () => {
    const onchange = setup(["a", "b", "c"]);
    const grip = (item: string) => screen.getByRole("button", { name: new RegExp(`^Move ${item},`) });
    await fireEvent.keyDown(grip("b"), { key: "ArrowUp" });
    expect(onchange).toHaveBeenLastCalledWith(["b", "a", "c"]);
    await fireEvent.keyDown(grip("b"), { key: "ArrowDown" });
    expect(onchange).toHaveBeenLastCalledWith(["a", "c", "b"]);
    onchange.mockClear();
    await fireEvent.keyDown(grip("a"), { key: "ArrowUp" });
    await fireEvent.keyDown(grip("c"), { key: "ArrowDown" });
    expect(onchange).not.toHaveBeenCalled();
  });

  it("moves an entry to where it is dropped, and ignores what came from elsewhere", async () => {
    const onchange = setup(["a", "b", "c"]);
    const row = (item: string) => screen.getByText(item).closest("li")!;
    await fireEvent.drop(row("a"));
    expect(onchange).not.toHaveBeenCalled();
    await fireEvent.dragStart(row("a"));
    await fireEvent.drop(row("c"));
    expect(onchange).toHaveBeenCalledWith(["b", "c", "a"]);
  });

  it("offers only choices not yet in the list, and no typing", () => {
    setup(["direct"], ["direct", "google"]);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByRole("button", { name: "Add" })).toBeTruthy();
    expect(screen.getByText("direct")).toBeTruthy();
  });

  it("has nothing to add once every choice is in", () => {
    setup(["direct"], ["direct"]);
    expect(screen.queryByRole("button", { name: "Add" })).toBeNull();
  });
});
