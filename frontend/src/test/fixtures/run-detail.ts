import type { RunResource, RunStepSummary, TraceEventResource } from "@/lib/api/client";

let seqCounter = 0;
function nextSeq(): number {
  seqCounter += 1;
  return seqCounter;
}
function resetSeq(): void {
  seqCounter = 0;
}

function ts(offsetSeconds: number): string {
  return new Date(Date.UTC(2026, 8, 19, 10, 0, 0) + offsetSeconds * 1000).toISOString();
}

function event(overrides: Partial<TraceEventResource> & Pick<TraceEventResource, "kind">): TraceEventResource {
  return {
    seq: nextSeq(),
    ts: ts(0),
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
    ...overrides,
  };
}

function stepSummary(overrides: Partial<RunStepSummary> & Pick<RunStepSummary, "step_id" | "tool">): RunStepSummary {
  return {
    seq: 0,
    status: "succeeded",
    attempts: 1,
    retry_count: 0,
    verification_status: "passed",
    duration_ms: 1200,
    error: null,
    ...overrides,
  };
}

const RUN_ID = "8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b";

const PLAN = {
  plan_id: "plan-1",
  revision: 0,
  created_by: "rules",
  steps: [
    { step_id: "s1", tool: "search_leads", depends_on: [], rationale: "Find candidate leads matching the request.", optional: false, fanout: null },
    { step_id: "s2", tool: "research_company", depends_on: ["s1"], rationale: "Enrich each lead's company.", optional: false, fanout: null },
    { step_id: "s3", tool: "score_lead", depends_on: ["s2"], rationale: "Rank leads by fit.", optional: false, fanout: null },
    { step_id: "s4", tool: "draft_outreach", depends_on: ["s3"], rationale: "Draft outreach copy for the top lead.", optional: false, fanout: null },
    { step_id: "s5", tool: "save_draft", depends_on: ["s4"], rationale: "Persist the draft before it can be sent.", optional: false, fanout: null },
    { step_id: "s6", tool: "send_email_mock", depends_on: ["s5"], rationale: "Send the approved outreach email.", optional: false, fanout: null },
    { step_id: "s7", tool: "update_customer", depends_on: ["s6"], rationale: "Mark the account as contacted.", optional: true, fanout: null },
  ],
};

/**
 * A run mid-flight: s1 ran clean, s2 failed once and retried into a second
 * attempt that succeeded, s3-s5 ran clean, and s6 is paused waiting on
 * human approval — so s6 and s7 have no actual execution entry yet. One
 * fixture exercises retries, the approval gap, and planned-but-unrun steps
 * together (used for most component and browser verification).
 */
export function buildRunWithRetryAndApproval(): { run: RunResource; events: TraceEventResource[] } {
  resetSeq();
  const approvalId = "a1b2c3d4-e5f6-4a1b-8c9d-0e1f2a3b4c5d";

  const events: TraceEventResource[] = [
    event({ kind: "run_created", status: "created", ts: ts(0) }),
    event({ kind: "run_started", status: "running", ts: ts(1) }),
    event({ kind: "plan_created", node: "plan", ts: ts(1), payload: { revision: 0 } }),

    event({ kind: "tool_started", node: "execute_tool", tool: "search_leads", step_id: "s1", attempt: 1, retry_count: 0, status: "started", input: { industry: "fintech", location: "London", limit: 3 }, ts: ts(2) }),
    event({ kind: "tool_succeeded", node: "execute_tool", tool: "search_leads", step_id: "s1", attempt: 1, retry_count: 0, status: "succeeded", duration_ms: 180, output: { leads: [{ lead_id: "L-104", company_id: "C-9" }], total_matched: 1, truncated: false }, ts: ts(2.2) }),
    event({ kind: "verification_passed", node: "verify", tool: "search_leads", step_id: "s1", attempt: 1, status: "passed", duration_ms: 5, payload: { mode: "invariant", checks: [{ name: "limit_respected", passed: true, expected: "<= 3", observed: 1 }], detail: null, verification_attempt: 1 }, ts: ts(2.3) }),

    event({ kind: "tool_started", node: "execute_tool", tool: "research_company", step_id: "s2", attempt: 1, retry_count: 0, status: "started", input: { company_id: "C-9" }, ts: ts(3) }),
    event({
      kind: "tool_failed",
      node: "execute_tool",
      tool: "research_company",
      step_id: "s2",
      attempt: 1,
      retry_count: 0,
      status: "failed",
      duration_ms: 3000,
      error: { class: "transient", message: "upstream enrichment provider timed out" },
      ts: ts(6),
    }),
    event({ kind: "retry_scheduled", node: "recover", tool: "research_company", step_id: "s2", attempt: 1, retry_count: 1, status: "execute_tool", severity: "warning", payload: { target: "execute_tool", delay_ms: 250, error_class: "transient" }, ts: ts(6.1) }),
    event({ kind: "tool_started", node: "execute_tool", tool: "research_company", step_id: "s2", attempt: 2, retry_count: 1, status: "started", input: { company_id: "C-9" }, ts: ts(6.4) }),
    event({ kind: "tool_succeeded", node: "execute_tool", tool: "research_company", step_id: "s2", attempt: 2, retry_count: 1, status: "succeeded", duration_ms: 210, output: { company_id: "C-9", name: "Northbridge Analytics", confidence: 0.82 }, ts: ts(6.6) }),
    event({ kind: "verification_passed", node: "verify", tool: "research_company", step_id: "s2", attempt: 2, status: "passed", duration_ms: 4, payload: { mode: "invariant", checks: [{ name: "confidence_in_range", passed: true, expected: "0..1", observed: 0.82 }], detail: null, verification_attempt: 1 }, ts: ts(6.7) }),

    event({ kind: "tool_started", node: "execute_tool", tool: "score_lead", step_id: "s3", attempt: 1, retry_count: 0, status: "started", input: { lead_id: "L-104" }, ts: ts(7) }),
    event({ kind: "tool_succeeded", node: "execute_tool", tool: "score_lead", step_id: "s3", attempt: 1, retry_count: 0, status: "succeeded", duration_ms: 30, output: { score: 87, band: "hot" }, ts: ts(7.1) }),
    event({ kind: "verification_passed", node: "verify", tool: "score_lead", step_id: "s3", attempt: 1, status: "passed", duration_ms: 3, payload: { mode: "invariant", checks: [{ name: "score_in_range", passed: true, expected: "0..100", observed: 87 }], detail: null, verification_attempt: 1 }, ts: ts(7.2) }),

    event({ kind: "tool_started", node: "execute_tool", tool: "draft_outreach", step_id: "s4", attempt: 1, retry_count: 0, status: "started", input: { lead_id: "L-104", tone: "direct" }, ts: ts(8) }),
    event({ kind: "tool_succeeded", node: "execute_tool", tool: "draft_outreach", step_id: "s4", attempt: 1, retry_count: 0, status: "succeeded", duration_ms: 640, output: { subject: "Quick question about Northbridge's onboarding flow", word_count: 96 }, ts: ts(8.7) }),
    event({ kind: "verification_passed", node: "verify", tool: "draft_outreach", step_id: "s4", attempt: 1, status: "passed", duration_ms: 4, payload: { mode: "invariant", checks: [{ name: "no_placeholders", passed: true, expected: "none", observed: "none" }], detail: null, verification_attempt: 1 }, ts: ts(8.8) }),

    event({ kind: "tool_started", node: "execute_tool", tool: "save_draft", step_id: "s5", attempt: 1, retry_count: 0, status: "started", input: { lead_id: "L-104" }, ts: ts(9) }),
    event({ kind: "tool_succeeded", node: "execute_tool", tool: "save_draft", step_id: "s5", attempt: 1, retry_count: 0, status: "succeeded", duration_ms: 90, output: { draft_id: "d_9182", version: 1 }, ts: ts(9.1) }),
    event({ kind: "verification_passed", node: "verify", tool: "save_draft", step_id: "s5", attempt: 1, status: "passed", duration_ms: 20, payload: { mode: "readback", checks: [{ name: "content_hash_matches", passed: true, expected: "abc123", observed: "abc123" }], detail: null, verification_attempt: 1 }, ts: ts(9.15) }),

    event({
      kind: "approval_requested",
      node: "request_approval",
      tool: "send_email_mock",
      step_id: "s6",
      status: "pending",
      payload: { approval_id: approvalId, args_hash: "hash-s6", risk: "high", expires_at: ts(86400 + 9.2) },
      ts: ts(9.2),
    }),
  ];

  const steps: RunStepSummary[] = [
    stepSummary({ seq: 1, step_id: "s1", tool: "search_leads", duration_ms: 180 }),
    stepSummary({ seq: 2, step_id: "s2", tool: "research_company", attempts: 2, retry_count: 1, duration_ms: 210 }),
    stepSummary({ seq: 3, step_id: "s3", tool: "score_lead", duration_ms: 30 }),
    stepSummary({ seq: 4, step_id: "s4", tool: "draft_outreach", duration_ms: 640 }),
    stepSummary({ seq: 5, step_id: "s5", tool: "save_draft", verification_status: "not_required", duration_ms: 90 }),
  ];

  const run: RunResource = {
    run_id: RUN_ID,
    parent_run_id: null,
    status: "awaiting_approval",
    status_reason: null,
    user_request: "Find the top 3 fintech leads in London, research them, score them, draft outreach to the best one and email it to them.",
    normalized_task: null,
    plan: PLAN,
    steps,
    pending_approval: {
      approval_id: approvalId,
      step_id: "s6",
      tool: "send_email_mock",
      risk: "high",
      title: "Send outreach email to Priya Nair",
      summary: "Sends the saved draft d_9182 to priya.nair@northbridge.example.",
      payload_preview: { to_email: "priya.nair@northbridge.example", draft_id: "d_9182", subject: "Quick question about Northbridge's onboarding flow" },
      args_hash: "hash-s6",
      expires_at: ts(86400 + 9.2),
    },
    counters: { step_count: 6, retry_total: 1, replan_count: 0 },
    resumable: true,
    final_response: null,
    timestamps: { created_at: ts(0), started_at: ts(1), finished_at: null, deadline_at: ts(300) },
    metadata: { planner_kind: "rules" },
  };

  return { run, events };
}

/**
 * A run that failed partway through: s1 succeeded, s2 exhausted its retry
 * budget and failed the run, so s3-s6 were never attempted. Exercises
 * planned-but-unrun steps caused by failure rather than an approval pause.
 */
export function buildFailedRunWithUnrunSteps(): { run: RunResource; events: TraceEventResource[] } {
  resetSeq();
  const runId = "2b1a0f9e-8d7c-4b6a-9f5e-4d3c2b1a0f9e";

  const events: TraceEventResource[] = [
    event({ kind: "run_created", status: "created", ts: ts(0) }),
    event({ kind: "run_started", status: "running", ts: ts(1) }),
    event({ kind: "plan_created", node: "plan", ts: ts(1) }),

    event({ kind: "tool_started", node: "execute_tool", tool: "search_leads", step_id: "s1", attempt: 1, retry_count: 0, status: "started", input: { industry: "fintech" }, ts: ts(2) }),
    event({ kind: "tool_succeeded", node: "execute_tool", tool: "search_leads", step_id: "s1", attempt: 1, retry_count: 0, status: "succeeded", duration_ms: 150, output: { leads: [{ lead_id: "L-201" }] }, ts: ts(2.15) }),
    event({ kind: "verification_passed", node: "verify", tool: "search_leads", step_id: "s1", attempt: 1, status: "passed", duration_ms: 3, payload: { mode: "invariant", checks: [], detail: null, verification_attempt: 1 }, ts: ts(2.2) }),

    event({ kind: "tool_started", node: "execute_tool", tool: "research_company", step_id: "s2", attempt: 1, retry_count: 0, status: "started", input: { company_id: "C-1" }, ts: ts(3) }),
    event({ kind: "tool_failed", node: "execute_tool", tool: "research_company", step_id: "s2", attempt: 1, retry_count: 0, status: "failed", duration_ms: 3000, error: { class: "transient", message: "upstream enrichment provider timed out" }, ts: ts(6) }),
    event({ kind: "retry_scheduled", node: "recover", tool: "research_company", step_id: "s2", attempt: 1, retry_count: 1, status: "execute_tool", severity: "warning", payload: { target: "execute_tool", delay_ms: 250, error_class: "transient" }, ts: ts(6.1) }),
    event({ kind: "tool_started", node: "execute_tool", tool: "research_company", step_id: "s2", attempt: 2, retry_count: 1, status: "started", input: { company_id: "C-1" }, ts: ts(6.4) }),
    event({ kind: "tool_failed", node: "execute_tool", tool: "research_company", step_id: "s2", attempt: 2, retry_count: 1, status: "failed", duration_ms: 3000, error: { class: "transient", message: "upstream enrichment provider timed out" }, ts: ts(9.4) }),
    event({ kind: "retry_scheduled", node: "recover", tool: "research_company", step_id: "s2", attempt: 2, retry_count: 2, status: "execute_tool", severity: "warning", payload: { target: "execute_tool", delay_ms: 500, error_class: "transient" }, ts: ts(9.5) }),
    event({ kind: "tool_started", node: "execute_tool", tool: "research_company", step_id: "s2", attempt: 3, retry_count: 2, status: "started", input: { company_id: "C-1" }, ts: ts(10) }),
    event({ kind: "tool_failed", node: "execute_tool", tool: "research_company", step_id: "s2", attempt: 3, retry_count: 2, status: "failed", duration_ms: 3000, error: { class: "transient", message: "upstream enrichment provider timed out" }, ts: ts(13) }),
    event({ kind: "budget_exhausted", node: "recover", tool: "research_company", step_id: "s2", status: "retries_exhausted", severity: "error", payload: { max_retries: 2 }, ts: ts(13.1) }),
    event({ kind: "run_failed", status: "failed", severity: "error", payload: { status_reason: "budget_exhausted" }, ts: ts(13.2) }),
  ];

  const steps: RunStepSummary[] = [
    stepSummary({ seq: 1, step_id: "s1", tool: "search_leads", duration_ms: 150 }),
    stepSummary({
      seq: 2,
      step_id: "s2",
      tool: "research_company",
      status: "failed",
      attempts: 3,
      retry_count: 2,
      verification_status: null,
      duration_ms: 3000,
      error: { class: "transient", message: "upstream enrichment provider timed out" },
    }),
  ];

  const run: RunResource = {
    run_id: runId,
    parent_run_id: null,
    status: "failed",
    status_reason: "budget_exhausted",
    user_request: "Research every lead in the SaaS segment and score them.",
    normalized_task: null,
    plan: PLAN,
    steps,
    pending_approval: null,
    counters: { step_count: 2, retry_total: 2, replan_count: 0 },
    resumable: false,
    final_response: null,
    timestamps: { created_at: ts(0), started_at: ts(1), finished_at: ts(13.2), deadline_at: ts(300) },
    metadata: { planner_kind: "rules" },
  };

  return { run, events };
}
