import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const AUTH = { Authorization: "Bearer e2e-token" };
const REQUIRED = ["model.fast", "model.strong", "model.embedding", "search.searxng.enabled", "search.searxng.url", "search.duckduckgo.enabled"];
const SET_UP = { "model.fast": "unused/fast", "model.strong": "unused/strong", "model.embedding": "unused/embed", "search.searxng.enabled": true, "search.searxng.url": "http://127.0.0.1:9" };

async function settings(request: APIRequestContext, changes: Record<string, unknown>) {
  const response = await request.patch("/v1/settings", { headers: AUTH, data: changes });
  expect(response.ok()).toBe(true);
  return response.json();
}

async function logIn(page: Page, path = "/") {
  await page.goto(path);
  await page.getByLabel("Token").fill("e2e-token");
  await page.getByRole("button", { name: "Log in" }).click();
}

// The service outlives each test, so each starts as a first start does.
test.beforeEach(async ({ request }) => {
  await settings(request, { ...Object.fromEntries(REQUIRED.map((key) => [key, null])), "research.max_searches": null, "runs.store": null });
});

// The image is read-only and offline, so anything from another origin would never load.
test("the first start asks for what is required, then the built page loads from the service alone", async ({ page, baseURL }) => {
  const origins = new Set<string>();
  page.on("request", (request) => origins.add(new URL(request.url()).origin));

  await logIn(page, "/#/runs");
  await expect(page.getByRole("status")).toContainText("unused");
  await page.getByLabel("Fast model").fill("unused/fast");
  await page.getByLabel("Strong model").fill("unused/strong");
  await page.getByLabel("Embedding model").fill("unused/embed");
  await page.getByLabel("SearXNG URL").fill("http://127.0.0.1:9");
  await page.getByRole("button", { name: "Save" }).click();

  // Nothing answers in this configuration, which the page says and does not stop at.
  await expect(page.getByRole("alert")).toContainText("provider unused, SearXNG");
  await page.getByRole("button", { name: "Continue anyway" }).click();
  await expect(page.getByText("Nothing has run yet.")).toBeVisible();
  await expect(page.getByText("0 run(s), 0 in flight")).toBeVisible();

  expect([...origins]).toEqual([new URL(baseURL!).origin]);
});

test("a wrong token is refused", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Token").fill("wrong");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("alert")).toHaveText("Wrong token.");
});

test("a value setup refuses is explained", async ({ page }) => {
  await logIn(page);
  await page.getByLabel("Fast model").fill("elsewhere/fast");
  await page.getByLabel("Strong model").fill("unused/strong");
  await page.getByLabel("Embedding model").fill("unused/embed");
  await page.getByLabel("SearXNG URL").fill("http://127.0.0.1:9");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("alert")).toContainText("unknown provider 'elsewhere'");
});

test("the setup asks for a search provider", async ({ page }) => {
  await logIn(page);
  await page.getByLabel("Fast model").fill("unused/fast");
  await page.getByLabel("Strong model").fill("unused/strong");
  await page.getByLabel("Embedding model").fill("unused/embed");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("alert")).toContainText("Still unset: a search provider.");
});

test("a scraped engine can be the setup's search provider, against a notice of its terms", async ({ page, request }) => {
  await logIn(page);
  await expect(page.getByText("These read each engine's own pages")).toBeVisible();
  await page.getByLabel("Fast model").fill("unused/fast");
  await page.getByLabel("Strong model").fill("unused/strong");
  await page.getByLabel("Embedding model").fill("unused/embed");
  await page.getByLabel("DuckDuckGo").check();
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("alert").first()).toContainText("provider unused");
  expect((await field(request, "search.duckduckgo.enabled")).value).toBe(true);
  expect((await field(request, "search.bing.enabled")).value).toBe(false);
});

test("the service settings group the search providers, with prices and the scraping notice", async ({ page, request }) => {
  await settings(request, SET_UP);
  await logIn(page);
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("tab", { name: "Service" }).click();

  for (const group of ["Web, with a key", "Web, without a key", "Web, scraped", "Specialized"]) {
    await expect(page.getByRole("heading", { name: group, exact: true })).toBeVisible();
  }
  await expect(page.getByText("Brave: $5 per 1,000 queries")).toBeVisible();
  await expect(page.getByText("These read each engine's own pages")).toBeVisible();
  await expect(page.getByLabel("Duckduckgo enabled", { exact: true })).not.toBeChecked();
});

test("a blank value does not end the setup", async ({ page }) => {
  await logIn(page);
  await page.getByLabel("Fast model").fill("   ");
  await page.getByLabel("Strong model").fill("unused/strong");
  await page.getByLabel("Embedding model").fill("unused/embed");
  await page.getByLabel("SearXNG URL").fill("http://127.0.0.1:9");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("alert")).toContainText("Still unset: model.fast");
  await expect(page.getByRole("heading", { name: "Set up kvasir" })).toBeVisible();
});

test("a secret setting is written but never shown", async ({ page, request }) => {
  await settings(request, SET_UP);
  await logIn(page);
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("tab", { name: "Service" }).click();

  const key = page.getByLabel("Brave api key", { exact: true });
  await expect(key).toHaveAttribute("placeholder", "not set");
  await key.fill("hidden");
  await key.blur();
  await expect(key).toHaveAttribute("placeholder", "set");
  await expect(key).toHaveValue("");
  expect((await field(request, "search.brave.api_key")).value).toEqual({ set: true });

  await page.getByRole("button", { name: "Reset brave api key to the default" }).click();
  await expect(key).toHaveAttribute("placeholder", "not set");
});

test("a service setting saves as it changes, and resets", async ({ page, request }) => {
  await settings(request, SET_UP);
  await logIn(page);
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("tab", { name: "Service" }).click();

  const searches = page.getByLabel("Max searches", { exact: true });
  await expect(searches).toHaveValue("16");
  await searches.fill("24");
  await searches.blur();
  await expect.poll(async () => field(request, "research.max_searches")).toMatchObject({ value: 24, stored: true });

  await page.getByRole("button", { name: "Reset max searches to the default" }).click();
  await expect(searches).toHaveValue("16");
  expect(await field(request, "research.max_searches")).toMatchObject({ value: 16, stored: false });

  await page.getByLabel("Store", { exact: true }).selectOption("sqlite");
  await expect(page.getByRole("status")).toContainText("runs.store");

  const fast = page.getByLabel("Fast", { exact: true });
  await fast.fill("elsewhere/fast");
  await fast.blur();
  await expect(page.getByRole("alert")).toContainText("unknown provider 'elsewhere'");
  await expect(fast).toHaveValue("unused/fast");
  expect((await field(request, "model.fast")).value).toBe("unused/fast");

  await fast.fill("");
  await fast.blur();
  await expect(page.getByRole("alert")).toContainText("Fast is required");
  await expect(fast).toHaveValue("unused/fast");

  await searches.fill("");
  await searches.blur();
  await expect(searches).toHaveValue("16");
});

test("a model is picked from a list of every model, or typed", async ({ page, request }) => {
  await settings(request, SET_UP);
  // The configured provider answers nothing here, so what it would list is given.
  await page.route("**/v1/providers", (route) => route.fulfill({ json: [{ name: "unused", reachable: true }] }));
  await page.route("**/v1/providers/unused/models", (route) =>
    route.fulfill({
      json: [
        { id: "fast", ref: "unused/fast", capabilities: ["completion"], context_length: 4096 },
        { id: "large", ref: "unused/large", capabilities: ["completion", "tools"], context_length: 8192 },
        { id: "embed", ref: "unused/embed", capabilities: ["embedding"] },
      ],
    }),
  );
  await logIn(page);
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("tab", { name: "Service" }).click();
  const fast = page.getByLabel("Fast", { exact: true });
  const options = page.getByRole("listbox").getByRole("option");

  // A filled field still offers the others, each named by its ref.
  await fast.click();
  await expect(options).toHaveCount(3);
  await expect(options.nth(1)).toContainText("unused/large");
  await expect(options.nth(1)).toContainText("completion, tools");

  // What is typed filters by the model's name, and not by what it can do.
  await fast.fill("comp");
  await expect(options).toHaveCount(0);
  await fast.fill("lar");
  await expect(options).toHaveCount(1);
  await options.first().click();
  await expect(fast).toHaveValue("unused/large");
  await expect.poll(async () => (await field(request, "model.fast")).value).toBe("unused/large");

  // A provider's model it did not list can be typed.
  await fast.fill("unused/not-listed");
  await page.getByRole("heading", { name: "Service" }).click();
  await expect.poll(async () => (await field(request, "model.fast")).value).toBe("unused/not-listed");
  await expect(fast).toHaveValue("unused/not-listed");

  // What was typed does not filter the list once left.
  await fast.click();
  await expect(options).toHaveCount(3);
  await page.keyboard.press("Escape");

  // A refused value is restored, and refused again when typed again.
  for (let attempt = 0; attempt < 2; attempt++) {
    await fast.fill("elsewhere/x");
    await page.getByRole("heading", { name: "Service" }).click();
    await expect(page.getByRole("alert")).toContainText("unknown provider 'elsewhere'");
    await expect(fast).toHaveValue("unused/not-listed");
  }
});

async function field(request: APIRequestContext, key: string) {
  const view = await (await request.get("/v1/settings", { headers: AUTH })).json();
  return view.fields.find((f: { key: string }) => f.key === key);
}

test("a person clears a search block from the page", async ({ page, request }) => {
  await settings(request, SET_UP);
  let challenges = [{ provider: "DuckDuckGo", url: "https://html.duckduckgo.com/html/?q=", expires_in: 600 }];
  await page.route("**/v1/search/challenges", (route) => route.fulfill({ json: challenges }));
  const sent: Record<string, unknown>[] = [];
  await page.routeWebSocket("**/v1/search/challenges/DuckDuckGo/live", (socket) => {
    socket.send(JSON.stringify({ frame: "/9j/4AAQ" }));
    socket.onMessage((message) => {
      const input = JSON.parse(String(message));
      sent.push(input);
      if (input.type === "text") {
        challenges = [];
        socket.send(JSON.stringify({ state: "cleared" }));
      }
    });
  });

  await logIn(page);
  await page.getByRole("button", { name: "Solve DuckDuckGo" }).click();
  const tab = page.getByRole("application", { name: "DuckDuckGo's page" });
  await expect(tab.locator("img")).toHaveAttribute("src", "data:image/jpeg;base64,/9j/4AAQ");

  await tab.click();
  await page.keyboard.press("a");
  await tab.evaluate((element) => {
    const clipboardData = new DataTransfer();
    clipboardData.setData("text", "pasted");
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData, bubbles: true }));
  });

  await expect(page.getByRole("application")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Solve DuckDuckGo" })).toHaveCount(0);
  // The middle of the picture, whatever size it is shown at, is the middle of the tab.
  const press = sent.find((input) => input.action === "press")!;
  expect(press.x).toBeCloseTo(640, -1);
  expect(press.y).toBeCloseTo(360, -1);
  expect(sent.filter((input) => input.type === "key" && input.down)).toEqual([{ type: "key", down: true, key: "a", code: "KeyA", key_code: 65, text: "a" }]);
  expect(sent.at(-1)).toEqual({ type: "text", text: "pasted" });
});
