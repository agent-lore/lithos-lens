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
 * Each test asserts its own capture landed on disk and is non-empty, so a
 * silently broken capture cannot pass.
 */

// Chromium's headless shell occasionally aborts a full-page capture with
// "Protocol error (Page.captureScreenshot)" under parallel load; one retry
// absorbs that environmental flake without masking real failures (the ready()
// waits below are deterministic).
test.describe.configure({ retries: 1 });

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
      await expect(page.locator('[data-task-group="open"]')).toBeVisible();
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

      const file = path.join(ARTIFACTS_DIR, `${slug}-${width}.png`);
      await page.screenshot({ path: file, fullPage: true });

      // A silently broken capture must not pass: the file has to exist and
      // carry actual image bytes.
      const stat = fs.statSync(file);
      expect(stat.size).toBeGreaterThan(0);
    });
  }
}
