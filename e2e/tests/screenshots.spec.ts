import { test, expect, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

/**
 * Responsive screenshot capture — the artifacts-dir contract.
 *
 * Every covered page is captured full-page at each of the four standard
 * viewport widths and written to a deterministic path:
 *
 *   e2e/artifacts/<page>-<width>.png
 *
 * That path layout IS the downstream contract: loom's visual-review flow
 * (agent-lore/lithos-loom#283) picks the images up from `e2e/artifacts/` by
 * exactly this naming scheme. Change it only in lockstep with that consumer.
 * The directory is gitignored; files are overwritten on every run.
 *
 * The contract is exact: each PNG's pixel width equals its stated viewport
 * width, and the page must not overflow horizontally (scrollWidth <= viewport
 * width) — both are asserted per capture, alongside the file existing with
 * non-empty bytes, so a silently broken capture cannot pass.
 */

// Chromium's headless shell occasionally aborts a full-page capture with
// "Protocol error (Page.captureScreenshot)" under parallel load. Mitigate the
// load itself: this file opts out of fullyParallel (mode "default" runs its
// tests sequentially in one worker) and captures only after a settled wait
// (fonts loaded + a double rAF tick). One retry stays as backstop; the ready()
// waits below are deterministic, so a retry can't mask a real failure.
test.describe.configure({ mode: "default", retries: 1 });

const ARTIFACTS_DIR = path.resolve(__dirname, "..", "artifacts");

const WIDTHS = [320, 768, 1024, 1440] as const;

const PAGES: ReadonlyArray<{
  slug: string;
  url: string;
  ready: (page: Page) => Promise<void>;
}> = [
  {
    slug: "dashboard",
    url: "/tasks?since=2026-08-01",
    ready: async (page) => {
      await expect(page.locator(".task-board")).toBeVisible();
      // The artifact must show the blocked fixture row WITH its styled
      // blocker chips (loom's visual review looks at this page).
      await expect(
        page.locator('[data-task-group="blocked"] .blocker-chip').first(),
      ).toBeVisible();
    },
  },
  {
    slug: "task-detail",
    url: "/tasks/influx-ingest-cutover",
    ready: async (page) => {
      await expect(
        page.locator('[data-task-detail="influx-ingest-cutover"]'),
      ).toBeVisible();
    },
  },
  {
    // The task-detail branch the demo task cannot show: no active claims and
    // no findings, so every empty state in the panel is on screen at once
    // (asked for by the round-3 artifact review, which could not confirm the
    // empty-state alignment from a task that has both).
    slug: "task-detail-empty",
    url: "/tasks/influx-dashboards",
    ready: async (page) => {
      await expect(
        page.locator('[data-task-detail="influx-dashboards"]'),
      ).toBeVisible();
      await expect(page.getByText("No active claims.")).toBeVisible();
    },
  },
  {
    slug: "note",
    url: "/note/note-influx-plan",
    ready: async (page) => {
      // The note page is only "ready" once the K1-S4 related aside is up too.
      await expect(
        page.getByRole("complementary", { name: "Related notes" }),
      ).toBeVisible();
    },
  },
  {
    slug: "note-quarantined",
    url: "/note/note-influx-legacy-ingest",
    ready: async (page) => {
      await expect(page.locator(".note-status-quarantined")).toBeVisible();
    },
  },
  {
    slug: "note-missing",
    url: "/note/missing-note",
    ready: async (page) => {
      await expect(page.getByText("Document not found.")).toBeVisible();
    },
  },
];

for (const { slug, url, ready } of PAGES) {
  for (const width of WIDTHS) {
    test(`screenshot: ${slug} at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      await page.goto(url);
      await ready(page);

      // No horizontal overflow: a page wider than the viewport would yield a
      // wider-than-stated PNG and a broken responsive layout.
      const scrollWidth = await page.evaluate(
        () => document.documentElement.scrollWidth,
      );
      expect(scrollWidth).toBeLessThanOrEqual(width);

      // Settle before capturing: fonts loaded plus a double rAF tick. This is
      // the real mitigation for Chromium's intermittent full-page
      // Page.captureScreenshot abort; the retry above is only a backstop.
      await page.evaluate(async () => {
        await document.fonts.ready;
        await new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)),
        );
      });

      const file = path.join(ARTIFACTS_DIR, `${slug}-${width}.png`);
      await page.screenshot({ path: file, fullPage: true });

      // A silently broken capture must not pass: the file has to exist,
      // carry actual image bytes, and be exactly the stated width (PNG IHDR
      // width, bytes 16-19 big-endian; deviceScaleFactor is 1).
      const stat = fs.statSync(file);
      expect(stat.size).toBeGreaterThan(0);
      const header = fs.readFileSync(file).subarray(0, 24);
      expect(header.readUInt32BE(16)).toBe(width);
    });
  }
}
