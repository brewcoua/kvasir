import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const AUTH = { Authorization: "Bearer e2e-token" };
const REQUIRED = ["model.fast", "model.strong", "model.embedding", "search.url"];
const SET_UP = { "model.fast": "unused/fast", "model.strong": "unused/strong", "model.embedding": "unused/embed", "search.url": "http://127.0.0.1:9" };

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
