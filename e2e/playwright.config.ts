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
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    // `uv run --directory ..` launches the packaged `lithos-lens` entry point
    // from the repo root regardless of Playwright's own cwd.
    command: "uv run --directory .. lithos-lens",
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      LITHOS_LENS_FAKE_LITHOS: "1",
      LITHOS_LENS_CONFIG: "lithos-lens.example.toml",
      LENS_PORT: String(PORT),
    },
  },
});
