/**
 * Thin typed fetch layer over the OpsPilot control-plane API.
 *
 * Response shapes come from `./schema.d.ts` (generated from the backend's
 * live OpenAPI contract, never hand-written — see `npm run generate:api`).
 * The backend owns every agent semantic (architecture.md §3.1): this layer
 * only moves bytes and attaches types, it derives nothing.
 */
import type { paths } from "./schema";

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type JsonOf<T> = T extends { content: { "application/json": infer R } } ? R : never;
type Ok<T> = T extends { 200: infer R } ? JsonOf<R> : never;

export type Healthz = Ok<paths["/healthz"]["get"]["responses"]>;
export type RunListResponse = Ok<paths["/runs"]["get"]["responses"]>;
export type RunSummary = RunListResponse["items"][number];
export type RunResource = Ok<paths["/runs/{run_id}"]["get"]["responses"]>;
export type RunStepSummary = NonNullable<RunResource["steps"]>[number];
export type RunPendingApproval = NonNullable<RunResource["pending_approval"]>;
export type ApprovalQueueResponse = Ok<paths["/approvals/queue"]["get"]["responses"]>;
export type ApprovalResource = ApprovalQueueResponse[number];
export type ApprovalDecisionRequest = NonNullable<
  paths["/approvals/{approval_id}/decision"]["post"]["requestBody"]
>["content"]["application/json"];
export type ApprovalDecisionKind = ApprovalDecisionRequest["decision"];

/** GET /runs query parameters, taken directly from the generated operation type. */
export type RunListQuery = NonNullable<paths["/runs"]["get"]["parameters"]["query"]>;
export type RunStatus = NonNullable<RunListQuery["status"]>[number];

export type TraceResponse = Ok<paths["/runs/{run_id}/trace"]["get"]["responses"]>;
export type TraceEventResource = TraceResponse["events"][number];
export type TraceEventKind = TraceEventResource["kind"];
export type TraceEventSeverity = TraceEventResource["severity"];
export type TraceQuery = NonNullable<paths["/runs/{run_id}/trace"]["get"]["parameters"]["query"]>;

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly statusText: string,
    public readonly body: unknown
  ) {
    super(`OpsPilot API request failed: ${status} ${statusText}`);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...init?.headers },
  });

  if (!response.ok) {
    const body = await response.json().catch(() => undefined);
    throw new ApiError(response.status, response.statusText, body);
  }

  return (await response.json()) as T;
}

export function getHealthz(): Promise<Healthz> {
  return request<Healthz>("/healthz");
}

export function listRuns(query: RunListQuery = {}): Promise<RunListResponse> {
  const params = new URLSearchParams();
  for (const s of query.status ?? []) params.append("status", s);
  if (query.since) params.set("since", query.since);
  if (query.until) params.set("until", query.until);
  if (query.parent_run_id) params.set("parent_run_id", query.parent_run_id);
  if (query.q) params.set("q", query.q);
  if (query.limit != null) params.set("limit", String(query.limit));
  if (query.cursor) params.set("cursor", query.cursor);

  const qs = params.toString();
  return request<RunListResponse>(`/runs${qs ? `?${qs}` : ""}`);
}

export function getRun(runId: string): Promise<RunResource> {
  return request<RunResource>(`/runs/${encodeURIComponent(runId)}`);
}

export function getRunTrace(runId: string, query: TraceQuery = {}): Promise<TraceResponse> {
  const params = new URLSearchParams();
  if (query.since_seq != null) params.set("since_seq", String(query.since_seq));
  if (query.limit != null) params.set("limit", String(query.limit));
  for (const k of query.kind ?? []) params.append("kind", k);
  if (query.severity_min) params.set("severity_min", query.severity_min);

  const qs = params.toString();
  return request<TraceResponse>(`/runs/${encodeURIComponent(runId)}/trace${qs ? `?${qs}` : ""}`);
}

export function listApprovalQueue(): Promise<ApprovalQueueResponse> {
  return request<ApprovalQueueResponse>("/approvals/queue");
}

export function getApproval(approvalId: string): Promise<ApprovalResource> {
  return request<ApprovalResource>(`/approvals/${encodeURIComponent(approvalId)}`);
}

/**
 * Records the operator's decision. `body.args_hash` should be the hash the
 * operator actually saw on screen (echoed back, never recomputed here —
 * the backend is the sole authority on what hash a set of arguments
 * canonicalizes to, and a mismatch is exactly the signal that this screen
 * has gone stale).
 */
export function decideApproval(
  approvalId: string,
  body: ApprovalDecisionRequest
): Promise<ApprovalResource> {
  return request<ApprovalResource>(`/approvals/${encodeURIComponent(approvalId)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
