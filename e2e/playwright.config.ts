import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright smoke suite for Lithos Lens.
 *
 * The `webServer` below boots the real application in **fake-Lithos app mode**
 * (`LITHOS_LENS_FAKE_LITHOS=1`), so the whole server-rendered UI is driven end
 * to end against the in-memory fixtures in `src/lithos_lens/fake_lithos.py` —
 * no Lithos MCP server required. `LITHOS_LENS_CONFIG` points at the checked-in
 * example config so discovery never depends on the developer's machine.
 *
 * Run with `make e2e` (installs deps + Chromium), or from this directory:
 *   npm install && npm run install-browsers && npm test
 */

const PORT = Number(process.env.LENS_E2E_PORT ?? 8123);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "list" : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },
  projects: [
    {
      // Everything that DRIVES the application. Defined by exclusion, not by
      // filename: a new spec file must land in a project automatically, or it
      // is silently absent from CI rather than failing loudly. The only files
      // held out are the two sequenced phases below.
      name: "app",
      testIgnore: /(screenshots|live-events)\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      // Tests that publish an event mutating a FIXTURE row (T1-S6). Same hub
      // property as the capture phase below, one step worse: these do not just
      // ADD a synthetic row, they MOVE a row the read-only tests assert on, so
      // running them alongside fails whichever test was looking at it.
      //
      // Sequenced rather than merely isolated, for the reason spelled out
      // below: `fullyParallel` and a file's own `mode` order tests only WITHIN
      // a project.
      name: "live-events",
      testMatch: /live-events\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
      dependencies: ["app"],
    },
    {
      // Sequenced AFTER the driving phases, not merely isolated from them.
      //
      // There is ONE `webServer` for the whole run, and `EventHub.publish`
      // fans every event to every connected browser. So while a driving
      // phase's test publishes, any other open tab receives that synthetic
      // event too — and a capture tab that receives it photographs state the
      // application never rendered from its own data (`task.created` leaves a
      // "Freshly created task" row stuck loading; `task.reopened` moves a
      // fixture row out of Completed). That is fabricated UI state inside a
      // required visual gate: it can manufacture a false difference, or mask
      // a real regression behind one, non-deterministically at whichever
      // width happened to have a tab open.
      //
      // `dependencies` is what makes this deterministic. `fullyParallel` and
      // this file's own `mode: "default"` order tests only WITHIN a project;
      // neither keeps the PHASES apart. The cost is that captures do not run
      // when a driving phase fails, which is the right trade for a gate whose
      // entire value is that its images can be trusted.
      name: "screenshots",
      testMatch: /screenshots\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
      // Behind BOTH driving phases. `live-events` is named explicitly rather
      // than leaned on transitively through `app`, so removing one edge cannot
      // silently let a fixture-mutating event land in a capture tab.
      dependencies: ["app", "live-events"],
    },
  ],
  webServer: {
    // `uv run --directory ..` launches the packaged `lithos-lens` entry point
    // from the repo root regardless of Playwright's own cwd.
    command: "uv run --directory .. lithos-lens",
    url: BASE_URL,
    // Correctness beats convenience: never silently reuse a stale server —
    // explicit opt-in for dev iteration via LENS_E2E_REUSE_SERVER=1.
    reuseExistingServer: process.env.LENS_E2E_REUSE_SERVER === "1",
    timeout: 120_000,
    env: {
      LITHOS_LENS_FAKE_LITHOS: "1",
      LITHOS_LENS_CONFIG: "lithos-lens.example.toml",
      LENS_PORT: String(PORT),
    },
  },
});
