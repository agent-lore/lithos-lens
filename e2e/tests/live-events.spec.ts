import { test, expect } from "@playwright/test";

/**
 * Live-event tests that mutate a FIXTURE row.
 *
 * The fake-mode publish seam feeds the real hub, and the hub fans every event
 * out to *every* connected browser — so a test that moves a fixture row moves
 * it under every dashboard the suite happens to have open, failing whichever
 * read-only test was asserting on it. These tests therefore run as their own
 * Playwright project (`live-events`, see playwright.config.ts), which depends
 * on `app` and so only starts once every read-only test has finished — and
 * which the `screenshots` capture phase in turn waits on, so a moved fixture
 * row can never be photographed.
 *
 * Serial within the file for the same reason: two of these running at once
 * would move the row under each other.
 */

test.describe.configure({ mode: "serial" });

test("task.reopened event moves a completed row out of the Completed group", async ({ page, request }) => {
  // T1-S6: the reopen lifecycle event drives the REAL SSE path. The row leaves
  // Completed live and waits in the pending strip, because which workable
  // section it belongs to now is the frontier's answer (the reconcile's).
  await page.goto("/tasks?since=2026-08-01");
  await expect(page.locator('[data-live-state="live"]')).toBeVisible();
  await expect(
    page.locator('[data-task-group="completed"] [data-task-row][data-task-id="lens-note-view"]'),
  ).toBeVisible();

  const publish = await request.post("/tasks/events/publish", {
    data: {
      id: `evt-e2e-reopen-${Date.now()}`,
      type: "task.reopened",
      task_id: "lens-note-view",
      payload: { agent: "worker-a", prior_status: "completed" },
      // Hold reconciliation off so the move is deterministically observable;
      // the fake dataset still reports the task as completed.
      requires_refresh: false,
    },
  });
  expect(publish.status()).toBe(202);

  const reopened = page.locator(
    '[data-task-list="pending"] [data-task-row][data-task-id="lens-note-view"]',
  );
  await expect(reopened).toBeVisible();
  await expect(reopened).toHaveAttribute("data-task-status", "open");
  await expect(reopened.locator(".badge").first()).toHaveText("open");
  await expect(
    page.locator('[data-task-group="completed"] [data-task-row][data-task-id="lens-note-view"]'),
  ).toHaveCount(0);
});

test("finding.posted reveals the finding chip on the task's row", async ({ page, request }) => {
  // The chip's whole affordance is absent-until-a-finding-lands, which only
  // means something if it starts hidden — the pipeline half of the `[hidden]`
  // regression covered in smoke.spec.ts. Mutates a fixture row, so it lives
  // here rather than alongside the read-only tests.
  await page.goto("/tasks?since=2026-08-01");
  await expect(page.locator('[data-live-state="live"]')).toBeVisible();

  const chip = page.locator(
    '[data-task-row][data-task-id="influx-ingest-cutover"] [data-finding-count]',
  );
  await expect(chip).toBeHidden();

  const publish = await request.post("/tasks/events/publish", {
    data: {
      id: `evt-e2e-finding-${Date.now()}`,
      type: "finding.posted",
      task_id: "influx-ingest-cutover",
      payload: { finding_id: "finding-e2e", agent: "worker-a" },
      // Hold reconciliation off so the optimistic chip is what we observe.
      requires_refresh: false,
    },
  });
  expect(publish.status()).toBe(202);

  await expect(chip).toBeVisible();
  await expect(chip).toHaveText("1 new finding");
});
