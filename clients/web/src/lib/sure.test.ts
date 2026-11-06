import { describe, expect, it } from "vitest";
import { asking, reply, sure } from "./sure.svelte";

describe("sure", () => {
  it("answers with what was replied, and closes", async () => {
    const answer = sure("Delete it?", "Delete");
    expect(asking.question).toMatchObject({ text: "Delete it?", action: "Delete" });
    reply(true);
    expect(await answer).toBe(true);
    expect(asking.question).toBeNull();
  });

  it("takes a question left open as a no when another replaces it", async () => {
    const first = sure("First?");
    const second = sure("Second?");
    expect(await first).toBe(false);
    reply(true);
    expect(await second).toBe(true);
  });
});
