import { expect, test, type Page } from "@playwright/test";

const run = (page: Page) => page.locator("section.panel", { has: page.locator("h2", { hasText: /^Run$/ }) });

test("lists every run and opens the one picked on its result", async ({ page }) => {
  await page.goto("/#/runs");
  // Other tests start runs on the same server, so only the fixtures are counted on.
  for (const state of ["done", "running", "failed", "rejected"]) {
    await expect(page.locator(`table.runs .state.${state}`).first()).toBeVisible();
  }

  await page.getByRole("link", { name: /What led to the recent backlash on Proton/ }).click();
  await expect(page).toHaveURL(/#\/runs\/report$/);
  await expect(run(page).locator(".heading")).toContainText("done · research");
  await expect(run(page).getByRole("tab", { name: "Result" })).toHaveAttribute("data-state", "active");

  await run(page).getByRole("tab", { name: "Overview" }).click();
  await expect(page).toHaveURL(/#\/runs\/report\/overview$/);
  await expect(run(page).getByRole("cell", { name: "verify" })).toBeVisible();
});

test("a link from before the pages were split still opens the run", async ({ page }) => {
  await page.goto("/#run/ranking/result");
  await expect(page).toHaveURL(/#\/runs\/ranking\/result$/);
  await expect(run(page).locator("table.heat")).toBeVisible();
});

test("an event opens the calls it made, and a call without kept content says so", async ({ page }) => {
  await page.goto("/#/runs/report/overview");
  const linked = run(page).locator(".events li", { has: page.locator(".link") }).first();
  const label = await linked.locator(".link").innerText();
  await linked.locator(".link").click();

  await expect(page).toHaveURL(/#\/runs\/report\/calls$/);
  const calls = run(page).locator("table.calls tr.call");
  await expect(run(page).locator(".filter", { hasText: "Calls after" })).toBeVisible();
  await expect(calls).toHaveCount(Number.parseInt(label));
  await calls.first().click();
  await expect(run(page).getByText("Content not kept.")).toBeVisible();

  await run(page).getByRole("button", { name: "Show all" }).click();
  await expect.poll(() => calls.count()).toBeGreaterThan(Number.parseInt(label));
});

test("a call recorded with its content shows what it sent and what came back", async ({ page }) => {
  await page.goto("/#/runs/wal/calls");
  await run(page).locator("table.calls tr.call").first().click();
  const content = run(page).locator("tr.content");
  await expect(content.getByRole("heading", { name: "user" })).toBeVisible();
  await expect(content.getByRole("heading", { name: "response" })).toBeVisible();
  await expect(content.locator("pre").first()).not.toBeEmpty();
});

test("usage shows how fast each model answered", async ({ page }) => {
  await page.goto("/#/runs/wal/usage");
  // 21,500 tokens out over 243.3 seconds of calls, in the recorded fixture.
  const model = run(page).getByRole("row", { name: /^openai\/gpt-oss/ });
  await expect(model.getByRole("cell", { name: "88.4" })).toBeVisible();
});

test("a ranking cell shows its quotes", async ({ page }) => {
  await page.goto("/#/runs/ranking/result");
  await run(page).locator("table.heat td.n:not(.empty)").first().hover();
  await expect(page.locator(".popover .quote").first()).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator(".popover")).toBeHidden();
});

test("a citation badge shows its number, and names its source on hover", async ({ page }) => {
  await page.goto("/#/runs/report/result");
  const badge = page.locator(".cite").first();
  await expect(badge).toHaveText(/^\d+$/);
  await badge.hover();
  await expect(page.locator(".popover")).toContainText("http");
});

test("a research run asks its questions first, and sends the settings changed", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Settings").click();
  const settings = page.getByRole("dialog");
  await settings.getByLabel("Searches", { exact: true }).fill("7");
  await settings.getByLabel("Searches", { exact: true }).press("Tab");
  await expect(settings.locator(".setting.changed")).toHaveCount(1);
  await page.keyboard.press("Escape");
  // The plan on the page follows the settings.
  await expect(page.locator(".plan")).toContainText("Up to 7 searches");

  await page.getByLabel("Question or topic").fill("Which phone?");
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByText("Before starting")).toBeVisible();
  await page.getByLabel("Which region or market should the answer cover?").fill("Europe");

  const request = page.waitForRequest((r) => r.url().endsWith("/v1/runs") && r.method() === "POST");
  await page.getByRole("button", { name: "Start" }).click();
  expect((await request).postDataJSON()).toEqual({
    method: "ledger",
    input: {
      question: "Which phone?",
      clarifications: [{ question: "Which region or market should the answer cover?", answer: "Europe" }],
    },
    options: { max_searches: 7 },
  });
  await expect(page).toHaveURL(/#\/runs\/[0-9a-f]+$/);
  await expect(run(page).locator(".heading")).toContainText("running · ledger · Which phone?");

  // The new run is followed live.
  const events = run(page).locator(".events li");
  await expect(events.first()).toBeVisible();
  const before = await events.count();
  await expect.poll(() => events.count()).toBeGreaterThan(before);
});

test("an article starts with its topic alone, from Enter", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("radio", { name: "Article" }).click();
  await expect(page.locator(".plan")).toContainText("3 perspectives");
  await page.getByLabel("Question or topic").fill("Kvasir");
  const request = page.waitForRequest((r) => r.url().endsWith("/v1/runs") && r.method() === "POST");
  await page.getByLabel("Question or topic").press("Enter");
  expect((await request).postDataJSON()).toEqual({ method: "storm", input: { question: "Kvasir" }, options: {} });
  await expect(run(page).locator(".heading")).toContainText("running · storm · Kvasir");
});

test("a ranking is asked for from the + menu, and shows as a badge", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("radio", { name: "Deep research" }).click();
  await page.getByRole("button", { name: "Add an option" }).click();
  await page.getByRole("menuitemradio", { name: /Ranking/ }).click();
  await expect(page.locator(".bar .badge")).toHaveText("Ranking");
  await expect(page.locator(".plan li", { hasText: "background" })).toContainText("unless the question names both");
  await expect(page.locator(".side .size")).toContainText("and 8 for empty cells");

  await page.getByLabel("Question or topic").fill("Rank the Pixel 9a and the Fairphone 5 on price and updates");
  await page.getByRole("button", { name: "Start" }).click();
  const request = page.waitForRequest((r) => r.url().endsWith("/v1/runs") && r.method() === "POST");
  await page.getByText("skip the questions").click();
  expect((await request).postDataJSON()).toEqual({
    method: "ledger",
    input: { question: "Rank the Pixel 9a and the Fairphone 5 on price and updates", clarifications: [] },
    options: { output: "ranking" },
  });
});

test("a removed badge leaves the output to the question", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("radio", { name: "Deep research" }).click();
  await page.getByRole("button", { name: "Add an option" }).click();
  await page.getByRole("menuitemradio", { name: /Report/ }).click();
  await expect(page.locator(".plan li", { hasText: "assess" })).toContainText("skipped for a report");
  await page.getByRole("button", { name: "Remove Report" }).click();
  await expect(page.locator(".bar .badge")).toHaveCount(0);
  await expect(page.locator(".plan li", { hasText: "assess" })).toContainText("only for a ranking or a table");
});

test("an article has no + menu", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("radio", { name: "Article" }).click();
  await expect(page.getByRole("button", { name: "Add an option" })).toHaveCount(0);
});

test("the method picked is kept, and its own runs are listed", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("radio", { name: "Article" }).click();
  await expect(page.locator(".recent")).toContainText("Kvasir");
  await expect(page.locator(".recent")).not.toContainText("Proton");
  await page.reload();
  await expect(page.getByRole("radio", { name: "Article" })).toHaveAttribute("data-state", "checked");
});

test("slash starts a new run from any page", async ({ page }) => {
  await page.goto("/#/runs/report");
  await expect(page.locator("nav.list a[aria-current=page]")).toContainText("Proton");
  await page.keyboard.press("/");
  await expect(page).toHaveURL(/#\/$/);
  await expect(page.getByLabel("Question or topic")).toBeFocused();
});

test("the theme is kept", async ({ page }) => {
  await page.goto("/");
  await page.locator(".select").click();
  await page.getByRole("option", { name: "amber" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "amber");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "amber");
});
