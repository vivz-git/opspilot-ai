import { ApiError } from "@/lib/api/client";

/** The request the demo is built around (docs/deployment.md §6). Offered as a
 * one-click fill in the submission form; the backend treats it like any other. */
export const CANONICAL_REQUEST =
  "Find the top 3 fintech leads in London, research their companies, score them, draft outreach to the best one and email it to them.";

/** `RunCreateRequest.user_request`'s server-side bound (§16.4). */
export const MAX_REQUEST_LENGTH = 4000;

/**
 * Operator-facing copy for the §13.1 codes `POST /runs` can answer with.
 * Anything else falls back to the server's own `detail`.
 */
const CODE_MESSAGES: Record<string, string> = {
  idempotency_conflict: "A run with this idempotency key already exists for a different request.",
  budget_exhausted: "The server refused a new run: a configured budget would be exceeded.",
  policy_violation:
    "The API refused the request at its access boundary. Reload the console to re-authenticate, then retry.",
};

export interface RunSubmissionError {
  message: string;
  /** The per-request correlation id the API echoes on every error (§13.1). */
  traceId: string | null;
}

function field(body: unknown, key: string): unknown {
  return body && typeof body === "object" && key in body
    ? (body as Record<string, unknown>)[key]
    : undefined;
}

export function describeRunSubmissionError(error: unknown): RunSubmissionError {
  if (!(error instanceof ApiError)) {
    return {
      message:
        error instanceof Error
          ? `${error.message}. Is the OpsPilot API reachable at the configured base URL?`
          : "Could not submit the run.",
      traceId: null,
    };
  }
  const code = field(error.body, "code");
  const detail = field(error.body, "detail");
  const traceId = field(error.body, "trace_id");
  const fieldErrors = field(error.body, "errors");

  let message: string;
  if (code === "validation_error") {
    const first = Array.isArray(fieldErrors) ? field(fieldErrors[0], "msg") : undefined;
    message = `The request was rejected: ${String(first ?? detail ?? "it failed validation")}.`;
  } else if (typeof code === "string" && CODE_MESSAGES[code]) {
    message = CODE_MESSAGES[code];
  } else {
    message = typeof detail === "string" ? detail : error.message;
  }
  return { message, traceId: typeof traceId === "string" ? traceId : null };
}
