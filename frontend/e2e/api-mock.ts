import type { Page, Route } from "@playwright/test";

/**
 * Route-level interception of the OpsPilot API (`NEXT_PUBLIC_API_BASE_URL`,
 * pinned to `http://127.0.0.1:9999` in `playwright.config.ts` — nothing
 * really listens there). Every e2e spec is deterministic and needs no live
 * backend or database, per architecture.md's "mocks only inside isolated
 * frontend tests / Playwright interception" guidance.
 */
export const API_BASE = "http://127.0.0.1:9999";

async function reply(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

/** Always-on baseline: the health indicator in the app shell polls this on every page. */
export async function mockHealthz(page: Page) {
  await mockJson(page, "/healthz", { status: "ok" });
}

/** Matches `path` exactly (ignoring any query string) under `API_BASE`. */
export async function mockJson(page: Page, path: string, body: unknown, status = 200) {
  await page.route(
    (url) => url.origin === new URL(API_BASE).origin && url.pathname === path,
    (route) => reply(route, body, status)
  );
}
