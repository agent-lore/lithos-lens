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

  // The terminal groups render their fixture rows (not just the always-present
  // section wrappers): ids, titles, counts, and terminal status metadata.
  const completed = page.locator('[data-task-group="completed"]');
  await expect(
    completed.locator('[data-task-row][data-task-id="lens-note-view"]'),
  ).toHaveAttribute("data-task-status", "completed");
  await expect(
    completed.getByRole("link", { name: "Land knowledge note view" }),
  ).toBeVisible();
  expect(await completed.locator("[data-task-row]").count()).toBe(1);

  const cancelled = page.locator('[data-task-group="cancelled"]');
  await expect(
    cancelled.locator('[data-task-row][data-task-id="influx-spike"]'),
  ).toHaveAttribute("data-task-status", "cancelled");
  await expect(
    cancelled.getByRole("link", { name: "Spike Influx client options" }),
  ).toBeVisible();
  expect(await cancelled.locator("[data-task-row]").count()).toBe(1);
});

test("clicking a task opens its detail page", async ({ page }) => {
  await page.goto("/tasks?since=2026-08-01");

  await page.getByRole("link", { name: "Cut over Influx ingest path" }).click();

  await expect(page).toHaveURL(/\/tasks\/influx-ingest-cutover/);
  await expect(
    page.locator('[data-task-detail="influx-ingest-cutover"]'),
  ).toBeVisible();
  // The claimed fixture surfaces its active claim on the detail page. The
  // agent appears in both the summary line and the claims list, so scope to
  // the first match rather than tripping strict mode.
  await expect(page.getByText("worker-a").first()).toBeVisible();
});

test("knowledge note renders server-side markdown", async ({ page }) => {
  await page.goto("/note/note-influx-plan");

  // Since K1-S1 the body is rendered markdown, so the fixture's `# Influx
  // migration plan` yields a second h1 inside .markdown-body — scope the
  // page-title assertion to the article header instead of tripping strict
  // mode on the duplicate.
  await expect(
    page.locator("article header").getByRole("heading", { name: "Influx migration plan" }),
  ).toBeVisible();
  // And the markdown really rendered (list items, not a plaintext <pre>).
  await expect(
    page.locator(".markdown-body").getByRole("listitem").first(),
  ).toContainText("Stage 1: dual-write");
});

test("knowledge note renders the related panel with edge badges", async ({ page }) => {
  // K1-S4: the note page carries a related <aside> fed by one lithos_related
  // call; the fixtures give the plan note a link, an unresolved contradicts
  // edge, and an unresolved provenance stub.
  await page.goto("/note/note-influx-plan");

  const panel = page.getByRole("complementary", { name: "Related notes" });
  await expect(panel).toBeVisible();
  await expect(
    panel.getByRole("link", { name: "Influx rollback route" }).first(),
  ).toBeVisible();
  await expect(panel.locator(".edge-direction")).toHaveText("incoming");
  await expect(panel.locator(".edge-conflict")).toHaveText("conflict: unresolved");
});

test("missing knowledge note shows the not-found banner", async ({ page }) => {
  // The fixture finding "finding-orphan" links knowledge_id=missing-note;
  // opening it must hit the real doc_not_found path, not a generic failure.
  await page.goto("/note/missing-note");

  await expect(page.getByText("Document not found.")).toBeVisible();
});

test("live-updates status banner is present on the dashboard", async ({ page }) => {
  await page.goto("/tasks?since=2026-08-01");
  await expect(page.locator("[data-live-status]")).toBeVisible();
});
