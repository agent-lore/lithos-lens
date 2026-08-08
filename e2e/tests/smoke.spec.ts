import { test, expect } from "@playwright/test";

/**
 * Smoke suite: the app boots in fake-Lithos mode and every top-level
 * server-rendered surface renders from the in-memory fixtures. These are
 * deliberately shallow — they prove the pages come up and are wired, not the
 * detailed view logic (that is covered by the Python unit suite).
 */

test("health endpoint reports Lithos ok", async ({ request }) => {
  const response = await request.get("/health");
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  expect(body.lithos).toBe("ok");
});

test("dashboard renders the task board with fixture rows", async ({ page }) => {
  await page.goto("/tasks?since=2026-08-01");

  await expect(page.getByRole("heading", { level: 1, name: "Tasks" })).toBeVisible();

  // The open group and its flagship fixture task are present.
  await expect(page.locator('[data-task-group="open"]')).toBeVisible();
  await expect(
    page.locator('[data-task-row][data-task-id="influx-ingest-cutover"]'),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Cut over Influx ingest path" }),
  ).toBeVisible();

  // At least one task row rendered overall.
  expect(await page.locator("[data-task-row]").count()).toBeGreaterThan(0);
});

test("clicking a task opens its detail page", async ({ page }) => {
  await page.goto("/tasks?since=2026-08-01");

  await page.getByRole("link", { name: "Cut over Influx ingest path" }).click();

  await expect(page).toHaveURL(/\/tasks\/influx-ingest-cutover/);
  await expect(
    page.locator('[data-task-detail="influx-ingest-cutover"]'),
  ).toBeVisible();
  // The claimed fixture surfaces its active claim on the detail page.
  await expect(page.getByText("worker-a")).toBeVisible();
});

test("knowledge note renders server-side markdown", async ({ page }) => {
  await page.goto("/note/note-influx-plan");

  await expect(
    page.getByRole("heading", { name: "Influx migration plan" }),
  ).toBeVisible();
});

test("live-updates status banner is present on the dashboard", async ({ page }) => {
  await page.goto("/tasks?since=2026-08-01");
  await expect(page.locator("[data-live-status]")).toBeVisible();
});
