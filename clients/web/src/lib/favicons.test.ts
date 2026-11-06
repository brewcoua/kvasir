import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

class FakeImage {
  static made: FakeImage[] = [];
  onload = () => {};
  onerror = () => {};
  src = "";
  constructor() {
    FakeImage.made.push(this);
  }
}

describe("wantIcons", () => {
  const srcs = () => FakeImage.made.map((image) => image.src);

  beforeEach(() => {
    FakeImage.made = [];
    vi.stubGlobal("Image", FakeImage);
    vi.useFakeTimers();
    vi.resetModules();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("loads four at a time, in the order asked, and the next as one finishes", async () => {
    const { loaded, wantIcons } = await import("./favicons.svelte");
    wantIcons(["a", "b", "c", "d", "e"]);
    expect(srcs()).toEqual(["a", "b", "c", "d"].map((host) => `/v1/favicons/${host}`));
    FakeImage.made[0].onload();
    expect(loaded.has("a")).toBe(true);
    expect(srcs().at(-1)).toBe("/v1/favicons/e");
  });

  it("stops what was waiting when asked for something else, and never loads one twice", async () => {
    const { wantIcons } = await import("./favicons.svelte");
    wantIcons(["a", "b", "c", "d", "e"]);
    wantIcons(["a", "f"]);
    FakeImage.made[0].onload();
    expect(srcs().at(-1)).toBe("/v1/favicons/f");
    expect(srcs()).not.toContain("/v1/favicons/e");
    expect(srcs().filter((src) => src === "/v1/favicons/a")).toHaveLength(1);
  });

  it("asks once more after a failure, then gives up", async () => {
    const { loaded, wantIcons } = await import("./favicons.svelte");
    wantIcons(["a"]);
    FakeImage.made[0].onerror();
    expect(loaded.has("a")).toBe(false);
    await vi.advanceTimersByTimeAsync(65_000);
    expect(srcs()).toHaveLength(2);
    FakeImage.made[1].onerror();
    await vi.advanceTimersByTimeAsync(600_000);
    expect(srcs()).toHaveLength(2);
    wantIcons(["a"]);
    expect(srcs()).toHaveLength(2);
  });

  it("draws an icon that the retry found", async () => {
    const { loaded, wantIcons } = await import("./favicons.svelte");
    wantIcons(["a"]);
    FakeImage.made[0].onerror();
    await vi.advanceTimersByTimeAsync(65_000);
    FakeImage.made[1].onload();
    expect(loaded.has("a")).toBe(true);
  });
});
