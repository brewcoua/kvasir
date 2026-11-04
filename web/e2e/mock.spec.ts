import { expect, test, type Page } from "@playwright/test";
import type { ResearchResult, RunDetail } from "../src/lib/api";

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
  await expect(badge).toHaveAttribute("href", /^https?:\/\//);
  await expect(badge).toHaveAttribute("target", "_blank");

  const opened = page.waitForEvent("popup");
  await badge.click();
  expect((await opened).url()).toMatch(/^https?:\/\//);
  await expect(page).toHaveURL(/#\/runs\/report\/result$/);
});

test("a result marks unverified sentences, and lists what verification changed", async ({ page }) => {
  await page.goto("/#/runs/report/result");
  const badge = page.locator(".markdown .unverified").first();
  await expect(badge).toHaveText("unverified");
  await expect(badge).toHaveAttribute("title", /No model could judge/);

  const removed = page.locator("details", { has: page.locator("summary", { hasText: "Removed by verification" }) });
  await removed.locator("summary").click();
  await expect(removed.getByRole("cell", { name: "reason model" })).toBeVisible();
  await expect(removed.getByRole("cell", { name: "fast model" }).first()).toBeVisible();

  const revised = page.locator("details", { has: page.locator("summary", { hasText: "Rewritten by verification (1 sentences)" }) });
  await revised.locator("summary").click();
  await expect(revised.getByRole("cell", { name: /after a public outcry/ })).toBeVisible();

  const unverified = page.locator("details", { has: page.locator("summary", { hasText: "Unverified (1 sentences)" }) });
  await unverified.locator("summary").click();
  await expect(unverified.getByRole("cell", { name: "The reason model returned no readable verdict." })).toBeVisible();

  const evidence = page.locator("details", { has: page.locator("summary", { hasText: "Evidence (" }) });
  await evidence.locator("summary").click();
  await expect(evidence.getByText("from search snippet; page not fetched")).toHaveCount(1);
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

// The run is fetched, and then the snapshot its event stream opens with replaces it, so both carry the edit.
async function edited(page: Page, id: string, edit: (run: RunDetail) => void) {
  await page.route(`**/v1/runs/${id}`, async (route) => {
    const response = await route.fetch();
    const detail = await response.json();
    edit(detail);
    await route.fulfill({ response, json: detail });
  });
  await page.route(`**/v1/runs/${id}/events`, async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replace(/^data: (.*)$/m, (_, data) => {
      const detail = JSON.parse(data);
      edit(detail);
      return `data: ${JSON.stringify(detail)}`;
    });
    await route.fulfill({ response, body });
  });
}

test("steps lay a run out as stages that open into questions and attempts", async ({ page }) => {
  await page.goto("/#/runs/graph/steps");
  const cards = run(page).locator(".step-card");
  for (const stage of ["brief", "research", "write", "verify"]) {
    await expect(run(page).getByTestId(`step-stage:${stage}`)).toBeVisible();
  }
  // Stages and questions are open at first; a search's pages are not.
  await expect(cards.filter({ hasText: /^question/ }).first()).toBeVisible();
  await expect(cards.filter({ hasText: /^attempt/ }).first()).toBeVisible();
  await expect(cards.filter({ hasText: /^page/ })).toHaveCount(0);

  const before = await cards.filter({ hasText: /^attempt/ }).count();
  await run(page).getByTestId("step-q:1").locator(".step-head").click();
  await expect(cards.filter({ hasText: /^attempt/ })).toHaveCount(before - 1);
  await run(page).locator(".step-panel").getByRole("button", { name: "Close" }).click();
  await run(page).getByTestId("step-stage:research").locator(".step-head").click();
  await expect(cards.filter({ hasText: /^question/ })).toHaveCount(0);

  await run(page).getByTestId("step-stage:research").click();
  await expect(cards.filter({ hasText: /^question/ }).first()).toBeVisible();
  // Opening a step grows the research box around it; its cards stay inside. The box and the pan are animated, so it is polled.
  await expect
    .poll(async () => {
      const box = (await run(page).getByTestId("step-stage:research").boundingBox())!;
      for (const card of await run(page).locator(".step-card.question, .step-card.attempt").all()) {
        const at = (await card.boundingBox())!;
        if (at.x < box.x || at.y < box.y || at.x + at.width > box.x + box.width + 1 || at.y + at.height > box.y + box.height + 1) return false;
      }
      return true;
    })
    .toBe(true);
});

test("steps colour a card by its kind, mark it with an icon, and explain the kinds in a legend", async ({ page }) => {
  await page.goto("/#/runs/graph/steps");
  await expect(run(page).locator(".step-group.stage .step-head svg").first()).toBeVisible();
  await expect(run(page).locator(".step-card.question .step-head svg").first()).toBeVisible();
  const legend = run(page).locator(".step-legend");
  for (const kind of ["stage", "question", "attempt", "page", "section", "verdict", "event"]) {
    await expect(legend.locator(`li[data-kind="${kind}"] svg`)).toBeVisible();
  }
  for (const kind of ["raised", "drew", "removed", "corroborates", "contradicts"]) {
    await expect(legend.locator(`li[data-link="${kind}"] svg`)).toBeVisible();
  }
});

test("steps show question cards for a run whose questions only its result holds", async ({ page }) => {
  await page.goto("/#/runs/wal/steps");
  await expect(run(page).locator(".step-card, .step-group").filter({ hasText: /^question/ }).first()).toBeVisible();
});

test("question cards show their status", async ({ page }) => {
  await page.goto("/#/runs/graph/steps");
  await expect(run(page).locator(".step-card.question .question-status").first()).toBeVisible();
});

test("a step with calls opens them in the calls tab", async ({ page }) => {
  await page.goto("/#/runs/graph/steps");
  await run(page).locator(".step-card.attempt").first().click();
  await run(page).locator(".step-panel").getByRole("button", { name: /calls?$/ }).click();
  await expect(page).toHaveURL(/#\/runs\/graph\/calls$/);
  await expect(run(page).locator(".filter", { hasText: "Calls after" })).toBeVisible();
});

test("the split card raises each first question along one trunk", async ({ page }) => {
  await page.goto("/#/runs/graph/steps");
  await expect(run(page).locator(".step-card.split")).toBeVisible();
  for (const id of [1, 2, 3, 4, 5, 6]) await expect(run(page).locator(`.svelte-flow__edge[data-id="raised:e:1>q:${id}"]`)).toBeAttached();
  await expect(run(page).locator('.svelte-flow__edge[data-id^="raised:e:1>"]')).toHaveCount(6);
});

test("a question lists its claims in the panel", async ({ page }) => {
  await page.goto("/#/runs/graph/steps");
  await run(page).getByTestId("step-q:2").locator(".step-head").click();
  const panel = run(page).locator(".step-panel");
  await expect(panel.locator(".question-status")).toBeVisible();
  await expect(panel.locator(".claim-list li")).toHaveCount(5);
  await expect(panel.locator(".claim-list li").first()).toContainText("confidence");
  await panel.getByRole("button", { name: "Close" }).click();
  await expect(panel).toHaveCount(0);
});

test("selecting a stage shows its log and progress in the panel", async ({ page }) => {
  await page.goto("/#/runs/graph/steps");
  await run(page).getByTestId("step-stage:brief").click();
  const panel = run(page).locator(".step-panel");
  await expect(panel.locator(".step-log li")).toHaveText([/writing the research brief/]);
  await run(page).getByTestId("step-stage:verify").locator(".step-head").click();
  await expect(panel.getByTestId("step-progress")).toBeVisible();
  await expect(run(page).locator(".step-card.event")).toHaveCount(0);
});

test("hovering a card dims the leaf cards it is not linked to", async ({ page }) => {
  await page.goto("/#/runs/graph/steps");
  const node = (selector: string) => run(page).locator(".svelte-flow__node", { has: page.locator(selector) }).first();
  const attempt = run(page).locator(".step-card.attempt").first();
  // The first view is set once the pane has a size, which under load can move the card from under the pointer; hover again until it holds.
  await expect(async () => {
    await attempt.hover();
    await expect(node(".step-card.section")).toHaveClass(/dim/, { timeout: 500 });
  }).toPass();
  await expect(node(".step-card.attempt")).not.toHaveClass(/dim/);
  await expect(node('[data-testid="step-stage:research"]')).not.toHaveClass(/dim/);
  await page.mouse.move(0, 0);
  await expect(run(page).locator(".svelte-flow__node.dim")).toHaveCount(0);
});

test("claims that corroborate or contradict across questions are drawn as arcs and ringed on their cards", async ({ page }) => {
  await edited(page, "graph", (detail) => {
    const result = detail.result as ResearchResult;
    const questions = detail.questions.length > 0 ? detail.questions : result.questions;
    const claim = (question: number, i: number) => result.claims.find((c) => c.id === questions.find((q) => q.id === question)!.claims[i])!;
    claim(1, 0).corroborated_by.push(claim(3, 0).id);
    claim(1, 1).corroborated_by.push(claim(3, 1).id);
    claim(2, 0).contradicted_by.push(claim(4, 0).id);
  });
  await page.goto("/#/runs/graph/steps");
  const arcs = run(page).locator(".svelte-flow__edge.corroborates, .svelte-flow__edge.contradicts");
  await expect(arcs).toHaveCount(2);
  await expect(run(page).locator(".svelte-flow__edge.corroborates")).toHaveCount(1);
  await expect(run(page).locator(".arc-badge.corroborates")).toHaveText("2");
  await expect(run(page).locator(".arc-badge.contradicts")).toHaveText("1");
  await expect(run(page).locator(".dot.corroborates")).toHaveCount(4);

  await run(page).locator(".dot.contradicts").first().dispatchEvent("click");
  const picked = run(page).locator(".step-panel .claim-list li.picked");
  await expect(picked).toBeVisible();
  await expect(picked.locator(".rel.contradicts")).toHaveText("contradicted by");
});

test("a live run's current stage is marked as current", async ({ page }) => {
  await page.goto("/#/runs/graph-live/steps");
  await expect(run(page).locator(".step-card.stage.current, .step-group.stage.current")).toHaveCount(1);
});

test("a live run's edge into its current step is animated", async ({ page }) => {
  await page.goto("/#/runs/graph-live/steps");
  // The replay restarts as it ends, and its first stage has no edge into it.
  await expect(run(page).locator(".svelte-flow__edge.animated").first()).toBeAttached({ timeout: 20_000 });
});

test("the knowledge page lists a site's pages and opens one on its text", async ({ page }) => {
  await page.goto("/#/");
  await page.getByRole("navigation").getByRole("link", { name: "Knowledge" }).click();
  await expect(page).toHaveURL(/#\/knowledge$/);

  await page.getByRole("button", { name: "en.wikipedia.org" }).click();
  await page.getByRole("link", { name: "Roman Empire" }).click();

  await expect(page).toHaveURL(/#\/knowledge\/3f2a9c1d0b7e4a56$/);
  await expect(page.getByText("The emperor held supreme power.")).toBeVisible();
  await expect(page.getByText("related (1)")).toBeVisible();
});
