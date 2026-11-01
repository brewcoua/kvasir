import { expect, test } from "@playwright/test";

// The image is read-only and offline, so anything from another origin would never load.
test("the built page loads from the service alone", async ({ page, baseURL }) => {
  const origins = new Set<string>();
  page.on("request", (request) => origins.add(new URL(request.url()).origin));

  await page.goto("/#/runs");
  await expect(page.getByText("Nothing has run yet.")).toBeVisible();
  await expect(page.getByText("0 run(s), 0 in flight")).toBeVisible();

  expect([...origins]).toEqual([new URL(baseURL!).origin]);
});
