import type { ApprovalResource } from "@/lib/api/client";

/**
 * `ApprovalResource.payload_preview` is typed `{ [key: string]: unknown }`
 * by the generated schema — like `RunResource.plan` (FE-003's
 * `lib/plan.ts`), the backend itself treats it as opaque per-tool JSON, so
 * this describes the known runtime shape `app/agent/preview.py` builds for
 * the system's two gated tools (`send_email_mock`, `update_customer` —
 * architecture.md §8.3) rather than hand-typing a competing response
 * interface. Every field is read defensively; an unrecognized tool falls
 * back to a generic view (§4.1's "the args, in full" is always available).
 */

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

function num(v: unknown): number | null {
  return typeof v === "number" ? v : null;
}

function bool(v: unknown): boolean | null {
  return typeof v === "boolean" ? v : null;
}

export interface EmailPreview {
  kind: "email";
  to: string | null;
  draftId: string | null;
  leadId: string | null;
  subject: string | null;
  body: string | null;
  contentHash: string | null;
  args: unknown;
}

export interface CustomerFieldDiff {
  before: unknown;
  after: unknown;
}

export interface CustomerUpdatePreview {
  kind: "customer_update";
  customerId: string | null;
  accountName: string | null;
  primaryContact: string | null;
  email: string | null;
  reason: string | null;
  expectedVersion: number | null;
  currentVersion: number | null;
  versionMatch: boolean | null;
  diff: Record<string, CustomerFieldDiff>;
  args: unknown;
}

export interface GenericPreview {
  kind: "generic";
  args: unknown;
}

export type ParsedApprovalPreview = EmailPreview | CustomerUpdatePreview | GenericPreview;

function parseDiff(value: unknown): Record<string, CustomerFieldDiff> {
  if (typeof value !== "object" || value === null) return {};
  const out: Record<string, CustomerFieldDiff> = {};
  for (const [field, entry] of Object.entries(value as Record<string, unknown>)) {
    if (typeof entry === "object" && entry !== null && "before" in entry && "after" in entry) {
      const e = entry as { before: unknown; after: unknown };
      out[field] = { before: e.before, after: e.after };
    }
  }
  return out;
}

export function parseApprovalPreview(approval: ApprovalResource): ParsedApprovalPreview {
  const preview = approval.payload_preview as Record<string, unknown>;

  if (approval.tool === "send_email_mock") {
    return {
      kind: "email",
      to: str(preview.to_email) ?? str(preview.to),
      draftId: str(preview.draft_id),
      leadId: str(preview.lead_id),
      subject: str(preview.subject),
      body: str(preview.body),
      contentHash: str(preview.content_hash),
      args: preview.args,
    };
  }

  if (approval.tool === "update_customer") {
    return {
      kind: "customer_update",
      customerId: str(preview.customer_id),
      accountName: str(preview.account_name),
      primaryContact: str(preview.primary_contact),
      email: str(preview.email),
      reason: str(preview.reason),
      expectedVersion: num(preview.expected_version),
      currentVersion: num(preview.current_version),
      versionMatch: bool(preview.version_match),
      diff: parseDiff(preview.diff),
      args: preview.args,
    };
  }

  return { kind: "generic", args: preview.args ?? preview };
}
