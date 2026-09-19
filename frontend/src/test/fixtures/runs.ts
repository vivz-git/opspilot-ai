import type { RunListResponse, RunSummary } from "@/lib/api/client";

/**
 * Fixture data typed against the generated schema (never hand-invented
 * shapes) — used by tests, and to drive the Runs screen without a live
 * backend (FE-002 acceptance: "renders from fixture JSON with no backend").
 */
export function makeRunSummary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: "8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b",
    parent_run_id: null,
    status: "completed",
    status_reason: null,
    user_request: "Find the top 3 fintech leads in London and draft outreach",
    counters: { step_count: 7, retry_total: 0, replan_count: 0 },
    resumable: false,
    timestamps: {
      created_at: "2026-09-19T08:12:00Z",
      started_at: "2026-09-19T08:12:01Z",
      finished_at: "2026-09-19T08:17:00Z",
      deadline_at: "2026-09-19T08:17:00Z",
    },
    duration_ms: 299_000,
    metadata: { planner_kind: "rules" },
    ...overrides,
  };
}

export const RUNS_FIXTURE_PAGE_1: RunListResponse = {
  total_estimate: 4,
  next_cursor: "eyJjcmVhdGVkX2F0IjoiMjAyNi0wOS0xOVQwODoxMjowMFoiLCJpZCI6IjFhMmIzYzRkIn0",
  items: [
    makeRunSummary({
      run_id: "8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b",
      status: "running",
      user_request: "Find the top 3 fintech leads in London and draft outreach",
      counters: { step_count: 4, retry_total: 0, replan_count: 0 },
      duration_ms: null,
      timestamps: {
        created_at: "2026-09-19T10:00:00Z",
        started_at: "2026-09-19T10:00:01Z",
        finished_at: null,
        deadline_at: "2026-09-19T10:05:00Z",
      },
    }),
    makeRunSummary({
      run_id: "1a2b3c4d-5e6f-4a1b-8c9d-0e1f2a3b4c5d",
      status: "awaiting_approval",
      user_request: "Send the drafted email to the highest scoring lead",
      counters: { step_count: 6, retry_total: 1, replan_count: 0 },
      duration_ms: null,
      timestamps: {
        created_at: "2026-09-19T09:40:00Z",
        started_at: "2026-09-19T09:40:01Z",
        finished_at: null,
        deadline_at: "2026-09-19T09:45:00Z",
      },
    }),
  ],
};

export const RUNS_FIXTURE_PAGE_2: RunListResponse = {
  total_estimate: 4,
  next_cursor: null,
  items: [
    makeRunSummary({
      run_id: "9c8b7a6f-5e4d-4c3b-2a1f-0e9d8c7b6a5f",
      status: "completed",
      user_request: "Research companies for leads in the SaaS segment",
      counters: { step_count: 7, retry_total: 0, replan_count: 0 },
      duration_ms: 302_000,
    }),
    makeRunSummary({
      run_id: "2b1a0f9e-8d7c-4b6a-9f5e-4d3c2b1a0f9e",
      status: "failed",
      user_request: "Update customer record for account acc_4471",
      counters: { step_count: 2, retry_total: 3, replan_count: 1 },
      duration_ms: 41_000,
    }),
  ],
};

export const RUNS_FIXTURE_EMPTY: RunListResponse = {
  total_estimate: 0,
  next_cursor: null,
  items: [],
};
