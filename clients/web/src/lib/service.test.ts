import { describe, expect, it } from "vitest";
import type { SettingInfo } from "./api";
import { groupSearch } from "./service";

const field = (key: string) => ({ key }) as SettingInfo;

describe("groupSearch", () => {
  it("puts each provider's fields under it, and keeps what belongs to none", () => {
    const fields = ["search.top_k", "search.mojeek.enabled", "search.mojeek_html.enabled", "search.unknown.enabled"].map(field);

    const { rest, groups } = groupSearch(fields);
    const provider = (id: string) => groups.flatMap((group) => group.providers).find((found) => found.id === id)!;

    expect(rest.map((found) => found.key)).toEqual(["search.top_k", "search.unknown.enabled"]);
    expect(provider("mojeek").fields.map((found) => found.key)).toEqual(["search.mojeek.enabled"]);
    expect(provider("mojeek_html").fields.map((found) => found.key)).toEqual(["search.mojeek_html.enabled"]);
  });
});
