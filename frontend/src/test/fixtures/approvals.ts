import type { ApprovalQueueResponse, ApprovalResource } from "@/lib/api/client";

/** Fixture data typed against the generated schema — see fixtures/runs.ts. */
export function makeApproval(overrides: Partial<ApprovalResource> = {}): ApprovalResource {
  return {
    approval_id: "a1b2c3d4-e5f6-4a1b-8c9d-0e1f2a3b4c5d",
    run_id: "8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b",
    step_id: "s3",
    tool: "send_email_mock",
    risk: "high",
    title: "Send outreach email to jane@example.com",
    summary: "Sends the approved outreach email to the highest-scoring lead.",
    status: "pending",
    args_hash: "sha256:abc123",
    payload_preview: { to: "jane@example.com", subject: "Following up" },
    created_at: "2026-09-19T08:12:00Z",
    requested_at: "2026-09-19T08:12:00Z",
    expires_at: "2026-09-20T08:12:00Z",
    decided_at: null,
    decided_by: null,
    reason: null,
    ...overrides,
  };
}

export const APPROVAL_QUEUE_FIXTURE: ApprovalQueueResponse = [
  makeApproval(),
  makeApproval({
    approval_id: "b2c3d4e5-e5f6-4a1b-8c9d-0e1f2a3b4c5e",
    step_id: "s5",
    tool: "update_customer",
    risk: "high",
    title: "Update customer billing address",
    summary: "Patches the customer record with the new billing address.",
  }),
];
