import type { Route } from "@playwright/test";
import { expect, test } from "@playwright/test";

import { API_BASE, mockHealthz, mockJson } from "./api-mock";

const RUN_ID = "8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b";

function runResource(status: "running" | "completed") {
  return {
    run_id: RUN_ID,
    parent_run_id: null,
    status,
    status_reason: null,
    user_request: "Find the top 3 fintech leads in London",
    normalized_task: null,
    plan: null,
    steps: [],
    pending_approval: null,
    counters: { step_count: 1, retry_total: 0, replan_count: 0 },
    resumable: false,
    final_response: null,
    timestamps: {
      created_at: "2026-09-19T08:12:00Z",
      started_at: "2026-09-19T08:12:01Z",
      finished_at: status === "completed" ? "2026-09-19T08:12:05Z" : null,
      deadline_at: "2026-09-19T08:17:00Z",
    },
    metadata: {},
  };
}

const INITIAL_EVENT = {
  seq: 1,
  ts: "2026-09-19T08:12:01Z",
  kind: "plan_created",
  severity: "info",
  node: "plan",
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

function sseEvent(seq: number, kind: string): string {
  const body = { ...INITIAL_EVENT, seq, kind };
  return `event: ${kind}\nid: ${seq}\ndata: ${JSON.stringify(body)}\n\n`;
}

test.beforeEach(async ({ page }) => {
  await mockHealthz(page);
});

test("new trace events pushed over SSE appear without a page reload", async ({ page }) => {
  let runFetchCount = 0;
  await page.route(
    (url) => url.origin === new URL(API_BASE).origin && url.pathname === `/runs/${RUN_ID}`,
    (route: Route) => {
      runFetchCount += 1;
      const status = runFetchCount === 1 ? "running" : "completed";
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(runResource(status)),
      });
    }
  );
  await mockJson(page, `/runs/${RUN_ID}/trace`, {
    run_id: RUN_ID,
    events: [INITIAL_EVENT],
    next_seq: null,
    complete: true,
  });
  await page.route(
    (url) => url.origin === new URL(API_BASE).origin && url.pathname === `/runs/${RUN_ID}/events`,
    (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sseEvent(2, "tool_started") + sseEvent(3, "run_completed"),
      })
  );

  await page.goto(`/runs/${RUN_ID}`);
  // A generous timeout here: under a shared dev server compiling several
  // routes' first visit in parallel across e2e workers, the initial JS
  // chunk for this dynamic route can take a while to build — that's dev
  // server contention, not the SSE behavior under test.
  await expect(page.getByText("plan_created")).toBeVisible({ timeout: 20_000 });

  // Pushed live, never in the initial REST trace fetch above.
  await expect(page.getByText("tool_started")).toBeVisible();
  await expect(page.getByText("run_completed")).toBeVisible();

  // The terminal event invalidated the run resource, refetching it as
  // "completed" — the authoritative REST value, not a locally patched one.
  await expect(page.getByText("completed", { exact: true })).toBeVisible();
});
