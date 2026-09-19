import { defineConfig, devices } from "@playwright/test";

/**
 * E2E config (FE-005+). Every test drives the app against a real Next.js
 * dev server but intercepts `NEXT_PUBLIC_API_BASE_URL` requests with
 * `page.route` — architecture.md's "mocks only inside isolated frontend
 * tests / Playwright interception" — so these are deterministic and never
 * require a live backend or database.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:3100",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: {
          executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH || "/opt/pw-browsers/chromium",
        },
      },
    },
  ],
  webServer: {
    command: "npm run dev -- --port 3100",
    url: "http://127.0.0.1:3100",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { NEXT_PUBLIC_API_BASE_URL: "http://127.0.0.1:9999" },
  },
});
