import { defineConfig, devices } from "@playwright/test";

/**
 * Minimal Playwright setup — scoped to FE-004's approval interaction path.
 * The full "submit → timeline → approve → complete" e2e smoke against the
 * real stack (architecture.md §18.5) is FE-008's job, with the MSW-backed
 * fixture infrastructure that task formally owns. This config exists so
 * that scope has somewhere to run; it does not build that infrastructure
 * early. Tests here mock the API at the network layer per-spec (no live
 * backend in this environment) so results are deterministic.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:3100",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: { executablePath: "/opt/pw-browsers/chromium" },
      },
    },
  ],
  webServer: {
    command: "npm run dev -- --port 3100",
    url: "http://127.0.0.1:3100",
    reuseExistingServer: !process.env.CI,
    env: { NEXT_PUBLIC_API_BASE_URL: "http://127.0.0.1:9999" },
    timeout: 120_000,
  },
});
