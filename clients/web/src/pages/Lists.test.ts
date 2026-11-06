import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Lists from "./Lists.svelte";
import { request } from "../lib/api";

vi.mock("../lib/api", () => ({ request: vi.fn() }));
const answer = vi.mocked(request);

const list = (more: object) => ({
  id: "a",
  name: "A list",
  url: "https://lists.example/a.txt",
  entries: [],
  enabled: false,
  effect: "block",
  tag: "seo_spam",
  ttl_days: null,
  guard: true,
  except: [],
  group: "SEO and farms",
  licence: "none",
  custom: false,
  hosts: 0,
  skipped: 0,
  fetched_at: null,
  error: null,
  ...more,
});

describe("Lists", () => {
  beforeEach(() => {
    answer.mockReset();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("says when source trust is off", async () => {
    answer.mockRejectedValue(new Error("sources are off: set sources.enabled"));
    render(Lists);
    expect(await screen.findByText(/Source trust is off/)).toBeTruthy();
  });

  it("groups lists, shows a failed download, and turns a list on", async () => {
    answer.mockResolvedValue([list({ error: "status 404" })]);
    render(Lists);
    expect(await screen.findByText(/Last download failed: status 404/)).toBeTruthy();
    expect(screen.getByRole("heading", { name: /SEO and farms/ })).toBeTruthy();

    await fireEvent.click(screen.getByRole("switch", { name: "Use A list" }));
    expect(answer).toHaveBeenCalledWith("PATCH", "/v1/sources/lists/a", { enabled: true });
  });

  it("turns on only the lists of a group that are off", async () => {
    answer.mockResolvedValue([list({ id: "a" }), list({ id: "b", enabled: true })]);
    render(Lists);
    await fireEvent.click(await screen.findByRole("button", { name: "All on" }));
    expect(answer).toHaveBeenCalledWith("PATCH", "/v1/sources/lists/a", { enabled: true });
    expect(answer).not.toHaveBeenCalledWith("PATCH", "/v1/sources/lists/b", { enabled: true });
  });

  it("leaves a list in the config read-only", async () => {
    answer.mockResolvedValue([list({ group: "Config", name: "government" })]);
    render(Lists);
    await screen.findByText("government");
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
  });

  it("names the kind a list gives its sites, and offers no other effect for it", async () => {
    answer.mockResolvedValue([list({ group: "Official", name: "US .gov domains", effect: "kind:government" })]);
    render(Lists);
    await fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.getByText(/Marks its sites as government/)).toBeTruthy();
    expect(screen.getAllByLabelText("Effect")).toHaveLength(1);
  });

  it("explains a site by the lists naming it", async () => {
    answer.mockImplementation(async (_method, path) =>
      path.endsWith("/explain")
        ? { source: { kind: "unknown", tags: ["blocked"], tier: "avoid", label: "", qid: "", origin: "list" }, lists: [{ id: "a", name: "A list", effect: "block", state: "guard" }] }
        : [],
    );
    render(Lists);
    await fireEvent.input(await screen.findByLabelText("Site to try"), { target: { value: "farm.org" } });
    await fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByText(/spared by the guard/)).toBeTruthy();
    expect(answer).toHaveBeenCalledWith("GET", "/v1/sources/farm.org/explain");
  });
});
