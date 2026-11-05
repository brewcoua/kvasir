import { expect, test } from "@playwright/test";

// The image is read-only and offline, so anything from another origin would never load.
test("the built page loads from the service alone", async ({ page, baseURL }) => {
  const origins = new Set<string>();
  page.on("request", (request) => origins.add(new URL(request.url()).origin));

  await page.goto("/#/runs");
  await page.getByLabel("Token").fill("e2e-token");
  await page.getByRole("button", { name: "Log in" }).click();
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
