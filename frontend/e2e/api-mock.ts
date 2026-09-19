import type { Page, Route } from "@playwright/test";

/**
 * Route-level interception of the OpsPilot API (`NEXT_PUBLIC_API_BASE_URL`,
 * pinned to `http://127.0.0.1:9999` in `playwright.config.ts` — nothing
 * really listens there). Every e2e spec is deterministic and needs no live
 * backend or database, per architecture.md's "mocks only inside isolated
 * frontend tests / Playwright interception" guidance.
 */
export const API_BASE = "http://127.0.0.1:9999";

/**
 * The CORS headers the real backend sends (`CORSMiddleware` with an explicit
 * origin allowlist and `allow_credentials=True`, main.py). The client sends
 * every request with `credentials: "include"` so the access proxy's cookie
 * rides along in a hosted deployment, and a credentialed response may not
 * answer with `Access-Control-Allow-Origin: *` — so an interception that
 * wildcards the origin would pass here and fail against the real API.
 */
export const CORS_HEADERS: Record<string, string> = {
  // `playwright.config.ts` serves the app here; the real backend echoes the
  // single allowlisted origin the same way.
  "Access-Control-Allow-Origin": "http://127.0.0.1:3100",
  "Access-Control-Allow-Credentials": "true",
};

async function reply(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    headers: CORS_HEADERS,
    body: JSON.stringify(body),
  });
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
