import { test, expect, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import {
  WRITES_BASE_URL,
  WRITES_HUMAN_GATE,
  WRITES_TIMER_GATE,
} from "../servers";

/**
 * Curated writes, end to end (T3-W4): the Complete action on a human gate's
 * row, and the receipt the write answers with.
 *
 * Runs against the WRITES instance only (servers.ts): the fake keeps a
 * completed gate completed for the life of its process, so this spec must
 * never touch the server the other specs and the captures share. Serial, in
 * one test, because the order IS the subject — the row with its action is
 * photographed before the write, the receipt after it — and a gate can be
 * completed once.
 *
 * Captures follow the artifacts-dir contract (`<page>-<width>.png`, exact
 * width, no horizontal overflow) that `screenshots.spec.ts` documents, for
 * the artifacts the PRD's visual review asks of these slices:
 *   gate-complete-action-<w>.png  the gate row with Complete and the identity
 *   complete-receipt-<w>.png      the receipt banner above the reconciled board
 *   proceed-anyway-confirm-<w>.png  a timer gate's Proceed anyway confirm page
 *                                   (T3-W4b): its ready_at, the waiters it
 *                                   releases, and the one confirming form
 */
//
// No retry, deliberately: a retry would find the gate already completed by
// the failed attempt and fail on that instead of on the real cause.
test.describe.configure({ mode: "serial", retries: 0 });

const ARTIFACTS_DIR = path.resolve(__dirname, "..", "artifacts");
const WIDTHS = [320, 768, 1024, 1440] as const;
const BOARD = `${WRITES_BASE_URL}/tasks?project=influx&since=2026-08-01`;

async function capture(page: Page, slug: string, width: number) {
  await page.setViewportSize({ width, height: 800 });
  const scrollWidth = await page.evaluate(
    () => document.documentElement.scrollWidth,
  );
  expect(scrollWidth).toBeLessThanOrEqual(width);
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(resolve)),
    );
  });
  const file = path.join(ARTIFACTS_DIR, `${slug}-${width}.png`);
  await page.screenshot({ path: file, fullPage: true });
  const header = fs.readFileSync(file).subarray(0, 24);
  expect(header.readUInt32BE(16)).toBe(width);
}

test("a human gate is completed from its row and the receipt says so", async ({
  page,
}) => {
  await page.context().addCookies([
    { name: "lens_operator", value: "dave", url: WRITES_BASE_URL },
  ]);
  await page.goto(BOARD);

  const row = page.locator(
    `[data-gate-row][data-task-id="${WRITES_HUMAN_GATE}"]`,
  );
  const action = row.locator("[data-complete-action]");
  await expect(action).toHaveCount(1);
  await expect(action.locator("[data-complete-button]")).toHaveText("Complete");
  await expect(action.locator("[data-operator-id]")).toHaveText("dave");
  // Machine-owned gates get no direct action in this slice.
  await expect(
    page.locator(
      '[data-gate-row][data-gate-type="timer"] [data-complete-action]',
    ),
  ).toHaveCount(0);

  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 800 });
    await row.scrollIntoViewIfNeeded();
    await capture(page, "gate-complete-action", width);
  }

  // Clicking INTO the row's form must not open the side panel (the row
  // handler exempts forms), and the note is sent as the outcome.
  await page.setViewportSize({ width: 1440, height: 800 });
  // Run a reconcile and wait for the board fragment to have been SWAPPED —
  // not merely for the refresh's response headers, which arrive before
  // tasks.js has read the body, parsed it and replaced the fragment. The
  // completion signal is the page's own: `replaceFragment` dispatches
  // `lens:fragment-replaced` (detail.name) after the swap and after the
  // operator's drafts are restored, so every check below sees the new DOM.
  const reconcileNow = () =>
    page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          const swapped = (event: Event) => {
            if ((event as CustomEvent).detail?.name !== "dashboard-data") return;
            document.removeEventListener("lens:fragment-replaced", swapped);
            resolve();
          };
          document.addEventListener("lens:fragment-replaced", swapped);
          document.dispatchEvent(new Event("lens:reconcile"));
        }),
    );
  await action.locator("[data-complete-note]").fill("Window confirmed with on-call");
  await expect(page.locator("[data-task-panel]")).toHaveCount(0);

  // An agent's event re-renders the board while the operator is mid-note:
  // the fresh row's field must still hold what they typed.
  await reconcileNow();
  await expect(action.locator("[data-complete-note]")).toHaveValue(
    "Window confirmed with on-call",
  );

  // And a reconcile that lands WHILE the write is in flight replaces the row,
  // detaching the form that posted. htmx then fires the answer's HX-Trigger
  // on that detached form, where nothing hears it — so the board must still
  // be re-read at once, from the receipt slot the answer swaps into, and not
  // wait for the 800ms debounce behind the SSE `task.completed`.
  await page.evaluate(() => {
    (window as any).__postingForm = document.querySelector(
      `[data-gate-row] [data-complete-action]`,
    );
  });
  let detachedBeforeAnswer = false;
  await page.route("**/approve", async (route) => {
    await reconcileNow();
    // Sampled after the swap event, so it is the swap's result, not a race
    // with the refresh body still in flight.
    detachedBeforeAnswer = await page.evaluate(
      () => !(window as any).__postingForm.isConnected,
    );
    await route.continue();
  });
  // Both waits armed BEFORE the click, so a fast refresh cannot slip past:
  // the first refresh REQUEST that starts once the answer is in.
  let answeredAt = 0;
  const answered = page
    .waitForResponse((r) => r.url().endsWith("/approve"))
    .then(() => {
      answeredAt = Date.now();
    });
  const refreshAfterAnswer = page.waitForRequest(
    (request) =>
      answeredAt > 0 && request.headers()["x-lithos-lens-refresh"] === "tasks",
  );
  await action.locator("[data-complete-button]").click();
  await answered;
  await refreshAfterAnswer;
  const refreshDelayMs = Date.now() - answeredAt;
  expect(detachedBeforeAnswer).toBe(true);
  expect(refreshDelayMs).toBeLessThan(500);

  // The answer lands in the page's one receipt slot, outside the board...
  const receipt = page.locator("#write-receipt [data-write-receipt]");
  await expect(receipt).toBeVisible();
  await expect(receipt).toContainText("Completed gate");
  await expect(receipt).toContainText("Window confirmed with on-call");
  await expect(receipt.locator("[data-receipt-released]")).toBeVisible();
  // ...and the board reconciles from fresh reads: the gate leaves Gates.
  await expect(row).toHaveCount(0);
  await expect(page.locator("[data-task-panel]")).toHaveCount(0);

  for (const width of WIDTHS) {
    await capture(page, "complete-receipt", width);
  }
});

test("a timer gate is completed only through its Proceed anyway page", async ({
  page,
}) => {
  await page.context().addCookies([
    { name: "lens_operator", value: "dave", url: WRITES_BASE_URL },
  ]);
  await page.goto(BOARD);

  // The row offers the link and no direct form (T3-W4b).
  const row = page.locator(
    `[data-gate-row][data-task-id="${WRITES_TIMER_GATE}"]`,
  );
  await expect(row.locator("[data-complete-action]")).toHaveCount(0);
  const link = row.locator("[data-proceed-anyway-link]");
  await expect(link).toHaveCount(1);
  // An ordinary link: it navigates rather than opening the side panel.
  await link.click();
  await expect(page).toHaveURL(
    new RegExp(`/tasks/${WRITES_TIMER_GATE}/approve\\?next=`),
  );
  await expect(page.locator("[data-task-panel]")).toHaveCount(0);

  // The page states what would resolve the gate and what completing releases.
  await expect(page.locator("[data-timer-pending] time")).toHaveCount(1);
  const waiters = page.locator("[data-proceed-anyway-waiters]");
  await expect(waiters.locator("li")).not.toHaveCount(0);
  await expect(page.locator("[data-proceed-anyway-watcher]")).toHaveText(
    "Whatever watches this gate will find it closed.",
  );
  const form = page.locator("[data-proceed-anyway-form]");
  await expect(form.locator('input[name="confirm"]')).toHaveValue(
    "proceed-anyway",
  );

  for (const width of WIDTHS) {
    await capture(page, "proceed-anyway-confirm", width);
  }

  // Confirmed, it completes and returns to the board with the receipt; with
  // no note the recorded outcome says it was early and names the gate type.
  await page.setViewportSize({ width: 1440, height: 800 });
  await form.locator("[data-complete-button]").click();
  await expect(page).toHaveURL(/\/tasks\?.*receipt=/);
  const receipt = page.locator("#write-receipt [data-write-receipt]");
  await expect(receipt).toContainText("Completed early via Lens by dave");
  await expect(receipt).toContainText("timer gate had not resolved");
  // The released list is the receipt's own, read from what Lithos answered.
  // This demo timer's waiter has a second blocker, so it releases none; the
  // titled-release case is pinned in tests/test_proceed_anyway.py.
  await expect(receipt.locator("[data-receipt-released]")).toHaveAttribute(
    "data-receipt-released",
    "0",
  );
  await expect(row).toHaveCount(0);
});
