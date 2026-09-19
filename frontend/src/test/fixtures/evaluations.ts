import type {
  EvaluationResultResource,
  EvaluationRunListResponse,
  EvaluationRunResource,
} from "@/lib/api/client";

/** Fixture data typed against the generated schema — see fixtures/runs.ts. */
export function makeEvaluationRun(
  overrides: Partial<EvaluationRunResource> = {}
): EvaluationRunResource {
  return {
    evaluation_run_id: "e1a2b3c4-6b4d-4e2f-9a1b-7c8d9e0f1a2b",
    suite: "all",
    status: "completed",
    started_at: "2026-09-19T08:12:00Z",
    finished_at: "2026-09-19T08:12:04Z",
    planner_kind: "rules",
    git_sha: "abc1234",
    model_id: null,
    prompt_version: null,
    seed: 1337,
    case_count: 7,
    passed: 6,
    failed: 1,
    metrics: {
      case_pass_rate: 6 / 7,
      task_success_rate: 1,
      avg_duration_ms: 812,
      p50_duration_ms: 700,
      p95_duration_ms: 1500,
      total_cases: 7,
      passed_cases: 6,
      failed_cases: 1,
    },
    ...overrides,
  };
}

export const EVALUATION_RUNS_FIXTURE: EvaluationRunListResponse = {
  items: [
    makeEvaluationRun(),
    makeEvaluationRun({
      evaluation_run_id: "f2b3c4d5-6b4d-4e2f-9a1b-7c8d9e0f1a2c",
      suite: "smoke",
      status: "running",
      finished_at: null,
      case_count: 0,
      passed: 0,
      failed: 0,
      metrics: {},
    }),
  ],
};

export function makeEvaluationResult(
  overrides: Partial<EvaluationResultResource> = {}
): EvaluationResultResource {
  return {
    result_id: "r1a2b3c4-6b4d-4e2f-9a1b-7c8d9e0f1a2b",
    evaluation_run_id: "e1a2b3c4-6b4d-4e2f-9a1b-7c8d9e0f1a2b",
    case_id: "happy_path_multi_step",
    run_id: "8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b",
    passed: true,
    assertions: [
      { name: "final_status", passed: true, detail: "" },
      { name: "tools_called[search_leads]", passed: true, detail: "dispatched 1x" },
    ],
    duration_ms: 812,
    retry_count: 0,
    tool_calls_count: 3,
    approval_outcome: null,
    failure_reason: null,
    ...overrides,
  };
}

export const EVALUATION_RESULTS_FIXTURE: EvaluationResultResource[] = [
  makeEvaluationResult(),
  makeEvaluationResult({
    result_id: "r2b3c4d5-6b4d-4e2f-9a1b-7c8d9e0f1a2c",
    case_id: "invalid_tool_result",
    passed: false,
    failure_reason: "assertion failed: output_validation",
    assertions: [
      { name: "final_status", passed: true, detail: "" },
      {
        name: "invariant[3] tool_call_matches_step",
        passed: false,
        detail: "expected 1 row, found 0",
        invariant: "3",
        evidence: { table: "opspilot.tool_calls", count: 0 },
      },
    ],
  }),
];
