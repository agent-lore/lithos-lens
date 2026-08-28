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
      name: "smoke",
      testMatch: /smoke\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      // Sequenced AFTER smoke, not merely isolated from it.
      //
      // There is ONE `webServer` for the whole run, and `EventHub.publish`
      // fans every event to every connected browser. So while smoke's
      // `task.created` test is running, any other open tab receives that
      // synthetic event too — and a capture tab that receives it photographs
      // a row the application never rendered from its own data ("Freshly
      // created task", stuck loading). That is fabricated UI state inside a
      // required visual gate: it can manufacture a false difference, or mask
      // a real regression behind one, non-deterministically at whichever
      // width happened to have a tab open.
      //
      // `dependencies` is what makes this deterministic. `fullyParallel` and
      // screenshots.spec.ts's own `mode: "default"` order tests only WITHIN a
      // project; neither keeps the two FILES apart. The cost is that captures
      // do not run when smoke fails, which is the right trade for a gate whose
      // entire value is that its images can be trusted.
      name: "screenshots",
      testMatch: /screenshots\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
      dependencies: ["smoke"],
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
