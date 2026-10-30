/**
 * Playwright configuration for the five DOD journeys.
 *
 * The web server is started by Playwright but the **API is not**: these journeys need the
 * seeded database behind the real API, and starting that from here would hide whether the
 * stack actually comes up. `make e2e` brings both up; running `npx playwright test`
 * against a dead API fails loudly with a connection error, which is the right outcome.
 */
import { defineConfig, devices } from "@playwright/test";

const WEB = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:3000";

export default defineConfig({
  testDir: "./tests/e2e",
  // Journeys share a signed-in session and read the same seeded data, so parallel workers
  // only add flakiness without buying speed on a demo-sized database.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: WEB,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // The console is a desktop tool; a NOC wall display is wider still.
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: process.env.PLAYWRIGHT_NO_SERVER
    ? undefined
    : {
        command: "npm run dev",
        url: WEB,
        reuseExistingServer: true,
        timeout: 180_000,
      },
});
