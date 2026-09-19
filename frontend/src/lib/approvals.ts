import { ApiError } from "@/lib/api/client";

/**
 * Operator-facing copy for the §13.1 RFC 9457 `code`s a decision can come
 * back with (architecture.md's approval conflict family, API-004). Each one
 * names a real, distinct reason a decision was refused — never collapsed
 * into a single generic "something went wrong".
 */
const CODE_MESSAGES: Record<string, string> = {
  approval_not_pending:
    "This approval is no longer pending — it may already have been decided by someone else.",
  approval_expired: "This approval expired before a decision was recorded.",
  approval_superseded: "A newer request for this step has superseded this approval.",
  run_not_resumable: "The run this approval belongs to is no longer resumable.",
  validation_error: "The decision was rejected: check the required fields below.",
};

export function describeDecisionError(error: unknown): string {
  if (error instanceof ApiError) {
    const code =
      error.body && typeof error.body === "object" && "code" in error.body
        ? String((error.body as { code?: unknown }).code)
        : null;
    const detail =
      error.body && typeof error.body === "object" && "detail" in error.body
        ? String((error.body as { detail?: unknown }).detail)
        : null;
    if (code && CODE_MESSAGES[code]) return CODE_MESSAGES[code];
    return detail ?? error.message;
  }
  return error instanceof Error ? error.message : "Unknown error.";
}
