import { defineConfig, devices } from "@playwright/test";
import {
  BASE_URL,
  PORT,
  TRUNCATED_BASE_URL,
  TRUNCATED_FRONTIER_LIMIT,
  TRUNCATED_PORT,
} from "./servers";

/**
 * Playwright smoke suite for Lithos Lens.
 *
 * The `webServer` entries below boot the real application in **fake-Lithos app
 * mode** (`LITHOS_LENS_FAKE_LITHOS=1`), so the whole server-rendered UI is
 * driven end to end against the in-memory fixtures in
 * `src/lithos_lens/fake_lithos.py` — no Lithos MCP server required.
 * `LITHOS_LENS_CONFIG` points at the checked-in example config so discovery
 * never depends on the developer's machine. There are two instances (ports and
 * rationale in `servers.ts`): the default board, and one running at a low
 * `frontier_limit` so the truncated board can be captured without degrading the
 * healthy one.
 *
 * Run with `make e2e` (installs deps + Chromium), or from this directory:
 *   npm install && npm run install-browsers && npm test
 */

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
      // The driving phases and every capture of the default board share ONE
      // `webServer` (the truncation instance is a separate process nothing
      // drives), and `EventHub.publish` fans every event to every browser
      // connected to that server. So while a driving
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
  // TWO instances (see servers.ts): the default board, and a second one whose
  // frontier_limit is low enough that the demo fixtures truncate. Separate
  // PROCESSES, so the second one also sits outside the event-leak problem the
  // projects above are sequenced around — `EventHub.publish` fans to the tabs
  // of ITS server only, and no driving phase talks to this one.
  //
  // BOTH pin `LENS_HOST` to loopback. Lens defaults to every interface, which
  // is the accepted posture for the container (REQUIREMENTS §5C.1) but the
  // wrong one here: fake mode registers `POST /tasks/events/publish`, an
  // unauthenticated write seam with no Origin check, so on a shared segment
  // anyone could fan an event into the very tabs this suite is photographing —
  // and those artifacts are read as evidence by loom's visual review. Pinned
  // per entry rather than left to a default, so a change to Lens's default
  // cannot quietly widen the harness. `tests/test_fake_lithos.py` asserts every
  // entry here carries it.
  webServer: [
    {
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
        LENS_HOST: "127.0.0.1",
      },
    },
    {
      command: "uv run --directory .. lithos-lens",
      url: TRUNCATED_BASE_URL,
      reuseExistingServer: process.env.LENS_E2E_REUSE_SERVER === "1",
      timeout: 120_000,
      env: {
        LITHOS_LENS_FAKE_LITHOS: "1",
        LITHOS_LENS_CONFIG: "lithos-lens.example.toml",
        LENS_PORT: String(TRUNCATED_PORT),
        LENS_HOST: "127.0.0.1",
        // The whole point of the second instance. Well clear of the env path's
        // `minimum = 1` floor, so the instance boots.
        LITHOS_LENS_TASKS_FRONTIER_LIMIT: TRUNCATED_FRONTIER_LIMIT,
      },
    },
  ],
});
