import { API_BASE_URL, type TraceEventKind } from "@/lib/api/client";

/**
 * `TraceEventKind` (§14.2) as a runtime array. The generated OpenAPI type is
 * compile-time only, but `GET /runs/{id}/events` (§13.4) names the SSE
 * `event:` field after the trace event's own `kind` — never a generic
 * `message` — so an `EventSource` listener must be registered per kind. This
 * mirrors `app/persistence/models.py::TraceEventKind` exactly, the same way
 * `status-badge.tsx`'s `TONE_BY_STATUS` mirrors the backend's status enums:
 * the frontend renders the backend's fixed vocabulary, it doesn't invent one.
 */
export const TRACE_EVENT_KINDS: readonly TraceEventKind[] = [
  "run_created",
  "run_started",
  "run_completed",
  "run_failed",
  "run_rejected",
  "run_expired",
  "run_cancelled",
  "node_entered",
  "node_exited",
  "plan_created",
  "plan_revised",
  "fanout_expanded",
  "tool_started",
  "tool_succeeded",
  "tool_failed",
  "tool_timeout",
  "tool_duplicate_suppressed",
  "retry_scheduled",
  "step_skipped",
  "budget_exhausted",
  "approval_requested",
  "approval_granted",
  "approval_rejected",
  "approval_expired",
  "approval_superseded",
  "verification_passed",
  "verification_failed",
  "verification_skipped",
  "policy_violation",
  "run_recovered",
];

/** §13.3/§14.2 terminal run statuses — arriving as a trace event means the
 * run itself is done, so the stream (and any client connection to it) ends
 * here, matching `app/execution/runs.py::TERMINAL_TRACE_KINDS`. */
export const TERMINAL_TRACE_EVENT_KINDS: ReadonlySet<TraceEventKind> = new Set([
  "run_completed",
  "run_failed",
  "run_rejected",
  "run_expired",
  "run_cancelled",
]);

/** §13.3 terminal run statuses, mirrored from `RunStatus` for the same reason. */
export const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "rejected",
  "expired",
  "cancelled",
]);

/**
 * Thin factory so tests can substitute a fake `EventSource` — the browser
 * global isn't available under jsdom, and injecting the constructor is
 * simpler and more explicit than a global polyfill.
 */
export function createRunEventSource(runId: string): EventSource {
  return new EventSource(`${API_BASE_URL}/runs/${encodeURIComponent(runId)}/events`);
}
