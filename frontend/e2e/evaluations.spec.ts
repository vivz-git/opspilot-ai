import { expect, test } from "@playwright/test";

import { mockHealthz, mockJson } from "./api-mock";

const RUN_ID = "e1a2b3c4-6b4d-4e2f-9a1b-7c8d9e0f1a2b";

const RUN = {
  evaluation_run_id: RUN_ID,
  suite: "all",
  status: "completed",
  started_at: "2026-09-19T08:12:00Z",
  finished_at: "2026-09-19T08:12:04Z",
  planner_kind: "rules",
  git_sha: "abc1234",
  model_id: null,
  prompt_version: null,
  seed: 1337,
  case_count: 2,
  passed: 1,
  failed: 1,
  metrics: { case_pass_rate: 0.5, task_success_rate: 1, avg_duration_ms: 800, total_cases: 2 },
};

const RESULTS = [
  {
    result_id: "r1",
    evaluation_run_id: RUN_ID,
    case_id: "happy_path_multi_step",
    run_id: "run-1",
    passed: true,
    assertions: [{ name: "final_status", passed: true, detail: "" }],
    duration_ms: 700,
    retry_count: 0,
    tool_calls_count: 3,
    approval_outcome: null,
    failure_reason: null,
  },
  {
    result_id: "r2",
    evaluation_run_id: RUN_ID,
    case_id: "invalid_tool_result",
    run_id: "run-2",
    passed: false,
    assertions: [
      {
        name: "invariant[3] tool_call_matches_step",
        passed: false,
        detail: "expected 1 row, found 0",
        invariant: "3",
      },
    ],
    duration_ms: 900,
    retry_count: 1,
    tool_calls_count: 2,
    approval_outcome: null,
    failure_reason: "assertion failed: output_validation",
  },
];

test.beforeEach(async ({ page }) => {
  await mockHealthz(page);
});

test("operator reviews an evaluation run and inspects a failing case", async ({ page }) => {
  await mockJson(page, "/evaluations/runs", { items: [RUN] });
  await mockJson(page, "/evaluations/metrics", {
    evaluation_run_id: RUN_ID,
    suite: "all",
    metrics: RUN.metrics,
  });
  await mockJson(page, `/evaluations/runs/${RUN_ID}`, RUN);
  await mockJson(page, `/evaluations/runs/${RUN_ID}/results`, RESULTS);

  await page.goto("/evaluations");
  await expect(page.getByRole("heading", { name: "Evaluations" })).toBeVisible();

  const row = page.getByRole("link", { name: `Open evaluation run ${RUN_ID}` });
  await expect(row).toBeVisible();
  await expect(row.getByText("completed")).toBeVisible();

  await row.click();
  await expect(page).toHaveURL(`/evaluations/${RUN_ID}`);
  await expect(page.getByText(RUN_ID)).toBeVisible();

  const failedCaseRow = page.getByRole("link", { name: "Inspect case invalid_tool_result" });
  await expect(failedCaseRow).toBeVisible();
  await expect(failedCaseRow.getByText("failed")).toBeVisible();

  await failedCaseRow.click();
  await expect(page.getByText("Failed assertions (1)")).toBeVisible();
  await expect(page.getByText("invariant[3] tool_call_matches_step")).toBeVisible();
  await expect(page.getByText("expected 1 row, found 0")).toBeVisible();
});

test("evaluations page shows an empty state with no runs", async ({ page }) => {
  await mockJson(page, "/evaluations/runs", { items: [] });
  await mockJson(page, "/evaluations/metrics", { evaluation_run_id: null, suite: null, metrics: {} });

  await page.goto("/evaluations");
  await expect(page.getByText(/No evaluation runs yet/)).toBeVisible();
});

test("evaluations page shows an error state when the API is unreachable", async ({ page }) => {
  await mockJson(page, "/evaluations/runs", { code: "internal_error" }, 500);
  await mockJson(page, "/evaluations/metrics", { evaluation_run_id: null, suite: null, metrics: {} });

  await page.goto("/evaluations");
  await expect(page.getByText("Could not load evaluation runs")).toBeVisible();
});

test("case table and run list are keyboard-navigable", async ({ page }) => {
  await mockJson(page, "/evaluations/runs", { items: [RUN] });
  await mockJson(page, "/evaluations/metrics", {
    evaluation_run_id: RUN_ID,
    suite: "all",
    metrics: RUN.metrics,
  });

  await page.goto("/evaluations");
  const row = page.getByRole("link", { name: `Open evaluation run ${RUN_ID}` });
  await row.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(`/evaluations/${RUN_ID}`);
});
