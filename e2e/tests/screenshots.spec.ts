import { test, expect, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { TRUNCATED_BASE_URL, TRUNCATED_FRONTIER_LIMIT } from "../servers";

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
      // …and the Gates section with a LIVE countdown. The server renders the
      // timer gate's absolute stamp ("ready at …") as the no-JS baseline and
      // tasks.js rewrites it to "ready in …", so waiting on that wording is
      // what makes the artifact show the countdown rather than the fallback.
      await expect(
        page.locator('[data-task-group="gates"] [data-gate-type-badge]').first(),
      ).toBeVisible();
      await expect(page.locator(".gate-countdown")).toContainText(/ready in /);
    },
  },
  {
    // The truncation half of T1-S11, which no reviewer had ever seen rendered:
    // the Not-classified tail, the accuracy banner, and the PER-COUNTER
    // "at least this many" marking. Served by the second webServer, whose
    // `frontier_limit` is low enough for the demo fixtures to overflow — an
    // absolute URL, so it is unambiguous which instance this shot came from.
    //
    // The `ready()` below waits on EVERY marker the picture exists to prove,
    // because this sandbox cannot look at the PNGs: a capture that silently
    // lost one must FAIL here rather than produce a healthy-looking screenshot
    // a later reviewer reads as proof that it did not.
    slug: "dashboard-truncated",
    url: `${TRUNCATED_BASE_URL}/tasks?since=2026-08-01`,
    ready: async (page) => {
      await expect(page.locator(".task-board")).toBeVisible();
      // 1. The overflow tail, with a row actually in it.
      await expect(
        page.locator('[data-task-group="unclassified"] .task-row').first(),
      ).toBeVisible();
      // 2. The accuracy banner, naming the one side that truncated.
      const banner = page.locator("[data-truncation-banner]");
      await expect(banner).toBeVisible();
      await expect(banner).toContainText(
        `ready frontier truncated at ${TRUNCATED_FRONTIER_LIMIT}`,
      );
      // 3. The per-counter marking on both counters the ready read feeds.
      await expect(
        page.locator('[data-approximate-count="ready"]'),
      ).toBeVisible();
      await expect(
        page.locator('[data-approximate-count="attention"]'),
      ).toBeVisible();
      // 4. The honesty claim itself, which is the whole slice: at this limit
      //    the blocked read answered IN FULL, so the Blocked card carries an
      //    exact count and must show no marking at all. A regression to the
      //    board-wide banner fails right here.
      await expect(
        page.locator('[data-approximate-count="blocked"]'),
      ).toHaveCount(0);
      await expect(
        page.locator('[data-task-group="blocked"] .task-row').first(),
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
    // The story's two headline surfaces in one page: the level-1 blocker chain
    // with live status, and the parent breadcrumb. `influx-ingest-cutover`
    // (captured above) has only OUTGOING edges, so it can never render either.
    slug: "task-detail-blocked",
    url: "/tasks/influx-backfill",
    ready: async (page) => {
      await expect(
        page.locator('[data-link-list="blockers"] li').first(),
      ).toBeVisible();
      await expect(page.locator("[data-parent-breadcrumb]")).toBeVisible();
    },
  },
  {
    // The children table and the `epic` type badge.
    slug: "task-detail-children",
    url: "/tasks/influx-epic",
    ready: async (page) => {
      await expect(
        page.locator(".children-table tbody tr").first(),
      ).toBeVisible();
    },
  },
  {
    // The overflow tail (T1-S7). The operator requirement is that an
    // agent-sized set degrade VISIBLY rather than truncate silently, which is
    // a claim about appearance and so cannot be settled by the HTML-substring
    // assertions in the pytest suite. `influx-shard-epic` is the demo's only
    // fixture that overflows a page (30 children against 25); the tail markup
    // is shared with the blocker chain and both provenance lists, so reviewing
    // it here reviews it everywhere.
    slug: "task-detail-overflow",
    url: "/tasks/influx-shard-epic",
    ready: async (page) => {
      await expect(page.locator('[data-link-tail="children"]')).toBeVisible();
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

      // Nothing here may have come from another spec's synthetic event. One
      // `webServer` serves the whole run and `EventHub.publish` fans to every
      // connected browser, so before the projects were sequenced (see
      // playwright.config.ts) smoke's `task.created` publish landed in THIS
      // tab and got photographed — a row the app never rendered from its own
      // data, inside a required visual gate. The config keeps the two files
      // apart; this asserts the result, so a later config change cannot
      // quietly reopen the hole. Deliberately matched on the skeleton CLASS
      // rather than smoke's task id: the fixtures never produce a skeleton, so
      // any skeleton here is a leak, whatever published it.
      await expect(page.locator(".task-row-skeleton")).toHaveCount(0);

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
