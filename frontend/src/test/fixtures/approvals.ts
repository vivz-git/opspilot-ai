import type { ApprovalQueueResponse, ApprovalResource } from "@/lib/api/client";

/**
 * Fixture data typed against the generated schema — modeled on the exact
 * `payload_preview` shapes `backend/app/agent/preview.py` builds for
 * `send_email_mock` and `update_customer` (never a hand-invented shape).
 */
export function makePendingEmailApproval(overrides: Partial<ApprovalResource> = {}): ApprovalResource {
  return {
    approval_id: "a1b2c3d4-1111-4a2b-8c3d-4e5f6a7b8c9d",
    run_id: "8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b",
    step_id: "s4",
    tool: "send_email_mock",
    risk: "high",
    title: "Send outreach email to jane@acmecorp.com",
    summary: "Sends the drafted outreach email to the highest-scoring lead.",
    status: "pending",
    args_hash: "sha256:3f9a1c2b4d5e6f708192a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4",
    payload_preview: {
      action: "send_email",
      tool: "send_email_mock",
      risk: "high",
      to: "jane@acmecorp.com",
      to_email: "jane@acmecorp.com",
      draft_id: "draft-771",
      lead_id: "lead-4471",
      subject: "Quick question about your fintech stack",
      body: "Hi Jane,\n\nI noticed Acme Corp has been expanding its payments infrastructure this year — congrats on the Series B.\n\nWe help fintechs like yours cut reconciliation time by 40%. Worth a quick call this week?\n\nBest,\nOpsPilot",
      content_hash: "sha256:body-hash-abc123",
      args: { to: "jane@acmecorp.com", draft_id: "draft-771", lead_id: "lead-4471" },
    },
    created_at: "2026-09-19T09:40:00Z",
    requested_at: "2026-09-19T09:40:01Z",
    expires_at: "2026-09-19T09:55:00Z",
    decided_at: null,
    decided_by: null,
    reason: null,
    ...overrides,
  };
}

export function makePendingCustomerUpdateApproval(
  overrides: Partial<ApprovalResource> = {}
): ApprovalResource {
  return {
    approval_id: "b2c3d4e5-2222-4b3c-9d4e-5f6a7b8c9d0e",
    run_id: "2b1a0f9e-8d7c-4b6a-9f5e-4d3c2b1a0f9e",
    step_id: "s2",
    tool: "update_customer",
    risk: "medium",
    title: "Update customer record for account acc_4471",
    summary: "Applies the reconciled contact fields to the customer record.",
    status: "pending",
    args_hash: "sha256:9a8b7c6d5e4f30211a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708",
    payload_preview: {
      action: "update_customer",
      tool: "update_customer",
      risk: "medium",
      customer_id: "acc_4471",
      account_name: "Acme Corp",
      primary_contact: "Jane Doe",
      email: "jane@acmecorp.com",
      reason: "Contact details changed after the lead responded with a corrected email.",
      expected_version: 3,
      current_version: 4,
      version_match: false,
      diff: {
        email: { before: "j.doe@acmecorp.com", after: "jane@acmecorp.com" },
        primary_contact: { before: "J. Doe", after: "Jane Doe" },
      },
      args: { customer_id: "acc_4471", email: "jane@acmecorp.com", primary_contact: "Jane Doe" },
    },
    created_at: "2026-09-19T08:20:00Z",
    requested_at: "2026-09-19T08:20:01Z",
    expires_at: "2026-09-19T08:35:00Z",
    decided_at: null,
    decided_by: null,
    reason: null,
    ...overrides,
  };
}

export function makeApprovedApproval(overrides: Partial<ApprovalResource> = {}): ApprovalResource {
  return makePendingEmailApproval({
    status: "approved",
    decided_at: "2026-09-19T09:42:00Z",
    decided_by: "operator@opspilot",
    ...overrides,
  });
}

export const APPROVAL_QUEUE_FIXTURE: ApprovalQueueResponse = [
  makePendingEmailApproval(),
  makePendingCustomerUpdateApproval(),
];

export const APPROVAL_QUEUE_EMPTY: ApprovalQueueResponse = [];
