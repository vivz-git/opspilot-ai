import type { Route } from "@playwright/test";
import { expect, test } from "@playwright/test";

import { API_BASE, CORS_HEADERS, mockHealthz, mockJson } from "./api-mock";

const RUN_ID = "5a0c7e21-3b9d-4f6a-8e12-4c7b9d0e1f23";
const CANONICAL =
  "Find the top 3 fintech leads in London, research their companies, score them, draft outreach to the best one and email it to them.";

/** What the real API answers `POST /runs` with for an auto-started run: the
 * durable row, `queued`, nothing executed yet (§13.2). */
function createdRun() {
  return {
    run_id: RUN_ID,
    parent_run_id: null,
    status: "queued",
    status_reason: null,
    user_request: CANONICAL,
    normalized_task: null,
    plan: null,
    steps: [],
    pending_approval: null,
    counters: { step_count: 0, retry_total: 0, replan_count: 0 },
    resumable: false,
    final_response: null,
    timestamps: {
      created_at: "2026-09-29T10:00:00Z",
      started_at: "2026-09-29T10:00:00Z",
      finished_at: null,
      deadline_at: "2026-09-29T10:05:00Z",
    },
    metadata: { planner_kind: "rules" },
  };
}

/** Where the run is by the time the detail page asks: paused at the gate. */
function pausedRun() {
  return {
    ...createdRun(),
    status: "awaiting_approval",
    resumable: true,
    counters: { step_count: 9, retry_total: 0, replan_count: 0 },
    pending_approval: {
      approval_id: "b7e1c2d3-4f5a-4b6c-9d8e-0f1a2b3c4d5e",
      step_id: "s8",
      tool: "send_email_mock",
      risk: "high",
      title: "Send outreach email",
      summary: "Sends the saved draft to the best-scoring lead.",
      payload_preview: { to_email: "lead@example.com" },
      args_hash: "sha256:feed",
      expires_at: "2026-09-30T10:00:00Z",
    },
  };
}

function event(seq: number, kind: string) {
  return {
    seq,
    ts: "2026-09-29T10:00:01Z",
    kind,
    severity: "info",
    node: null,
    tool: null,
    step_id: null,
    attempt: null,
    status: null,
    duration_ms: null,
    retry_count: null,
    error: null,
    payload: {},
    input: null,
    output: null,
  };
}

test.beforeEach(async ({ page }) => {
  await mockHealthz(page);
});

test("operator submits the canonical request and lands on the new run", async ({ page }) => {
  let posted: unknown = null;
  await page.route(
    (url) => url.origin === new URL(API_BASE).origin && url.pathname === "/runs",
    (route: Route) => {
      if (route.request().method() === "POST") {
        posted = route.request().postDataJSON();
        return route.fulfill({
          headers: CORS_HEADERS,
          status: 201,
          contentType: "application/json",
          body: JSON.stringify(createdRun()),
        });
      }
      return route.fulfill({
        headers: CORS_HEADERS,
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], next_cursor: null, total_estimate: 0 }),
      });
    }
  );
  await mockJson(page, `/runs/${RUN_ID}`, pausedRun());
  await mockJson(page, `/runs/${RUN_ID}/trace`, {
    run_id: RUN_ID,
    events: [event(1, "run_created"), event(2, "approval_requested")],
    next_seq: 3,
    complete: false,
  });
  await page.route(
    (url) => url.origin === new URL(API_BASE).origin && url.pathname === `/runs/${RUN_ID}/events`,
    (route: Route) =>
      route.fulfill({
        headers: CORS_HEADERS,
        status: 200,
        contentType: "text/event-stream",
        body: "",
      })
  );

  await page.goto("/runs");
  await expect(page.getByText("No runs yet. Submit a request above to start one.")).toBeVisible({
    timeout: 20_000,
  });

  const submit = page.getByRole("button", { name: "Submit run" });
  await expect(submit).toBeDisabled();
  await page.getByRole("button", { name: "Use the canonical request" }).click();
  await expect(page.getByLabel("New run")).toHaveValue(CANONICAL);
  await submit.click();

  await expect(page).toHaveURL(`/runs/${RUN_ID}`, { timeout: 20_000 });
  expect(posted).toEqual({ user_request: CANONICAL, auto_start: true });
  // The detail view renders what the server reports for the run it created.
  await expect(page.getByText(CANONICAL).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("awaiting_approval").first()).toBeVisible();
});

test("a refused submission shows the server's reason and stays on the runs page", async ({
  page,
}) => {
  await page.route(
    (url) => url.origin === new URL(API_BASE).origin && url.pathname === "/runs",
    (route: Route) => {
      if (route.request().method() === "POST") {
        return route.fulfill({
          headers: CORS_HEADERS,
          status: 409,
          contentType: "application/problem+json",
          body: JSON.stringify({
            type: "https://opspilot.dev/errors/budget-exhausted",
            title: "Budget exhausted",
            status: 409,
            detail: "over budget",
            instance: "/runs",
            code: "budget_exhausted",
            errors: [],
            trace_id: "0123456789abcdef0123456789abcdef",
          }),
        });
      }
      return route.fulfill({
        headers: CORS_HEADERS,
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], next_cursor: null, total_estimate: 0 }),
      });
    }
  );

  await page.goto("/runs");
  await page.getByLabel("New run").fill("Research company comp_northwind.");
  await page.getByRole("button", { name: "Submit run" }).click();

  // Scoped: Next.js renders its own `role="alert"` route announcer.
  const alert = page.getByRole("alert").filter({ hasText: "trace_id" });
  await expect(alert).toContainText("a configured budget would be exceeded", { timeout: 20_000 });
  await expect(alert).toContainText("trace_id 0123456789abcdef0123456789abcdef");
  await expect(page).toHaveURL("/runs");
});
