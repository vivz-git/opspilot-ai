"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createEvaluationRun,
  decideApproval,
  getApproval,
  getEvaluationMetrics,
  getEvaluationRun,
  getHealthz,
  getRun,
  getRunTrace,
  listApprovalQueue,
  listEvaluationResults,
  listEvaluationRuns,
  listRuns,
  listTools,
  type ApprovalDecisionRequest,
  type EvaluationMetricsQuery,
  type EvaluationRunCreateRequest,
  type EvaluationRunListQuery,
  type RunListQuery,
  type TraceEventResource,
} from "./client";

const TRACE_PAGE_LIMIT = 500;

/**
 * Fetch every persisted trace event for a run, once, by walking `next_seq`.
 *
 * `GET /runs/{id}/trace`'s `next_seq`/`complete` pair is designed for live
 * tailing (§13.4): on a non-terminal run, `complete` is never true and an
 * empty page still returns `next_seq = since_seq` so a poller knows to ask
 * again later. FE-003 has no live polling (that's FE-007) — it just needs
 * "everything persisted right now" for a reload-reconstructible view — so
 * the loop stops the moment a page makes no progress, not when `complete`
 * flips true.
 */
async function fetchFullTrace(runId: string): Promise<TraceEventResource[]> {
  const events: TraceEventResource[] = [];
  let sinceSeq = 1;

  for (;;) {
    const page = await getRunTrace(runId, { since_seq: sinceSeq, limit: TRACE_PAGE_LIMIT });
    events.push(...page.events);
    if (page.events.length === 0 || page.next_seq == null || page.next_seq === sinceSeq) {
      break;
    }
    sinceSeq = page.next_seq;
  }

  return events;
}

export function useHealthz() {
  return useQuery({
    queryKey: ["healthz"],
    queryFn: getHealthz,
    refetchInterval: 30_000,
  });
}

export function useRuns(query: RunListQuery = {}) {
  return useQuery({
    queryKey: ["runs", query],
    queryFn: () => listRuns(query),
    // Keep the previous page's rows on screen while the next page loads,
    // instead of collapsing to the loading skeleton on every filter/page
    // change — the pagination-loading state is `isFetching`, not `isPending`.
    placeholderData: keepPreviousData,
  });
}

export function useRun(runId: string) {
  return useQuery({
    queryKey: ["runs", runId],
    queryFn: () => getRun(runId),
    enabled: Boolean(runId),
  });
}

export function useRunTrace(runId: string) {
  return useQuery({
    queryKey: ["runs", runId, "trace"],
    queryFn: () => fetchFullTrace(runId),
    enabled: Boolean(runId),
  });
}

export function useApprovalQueue() {
  return useQuery({
    queryKey: ["approvals", "queue"],
    queryFn: listApprovalQueue,
    refetchInterval: 15_000,
  });
}

export function useApproval(approvalId: string) {
  return useQuery({
    queryKey: ["approvals", approvalId],
    queryFn: () => getApproval(approvalId),
    enabled: Boolean(approvalId),
  });
}

export function useDecideApproval(approvalId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ApprovalDecisionRequest) => decideApproval(approvalId, body),
    onSuccess: (updated) => {
      queryClient.setQueryData(["approvals", approvalId], updated);
      queryClient.invalidateQueries({ queryKey: ["approvals", "queue"] });
      queryClient.invalidateQueries({ queryKey: ["runs", updated.run_id], exact: true });
    },
  });
}

export function useTools() {
  return useQuery({
    queryKey: ["tools"],
    queryFn: listTools,
  });
}

/** A run that is still executing is polled; a terminal one never needs to be asked again. */
const ACTIVE_EVALUATION_STATUSES = new Set(["running"]);

export function useEvaluationRuns(query: EvaluationRunListQuery = {}) {
  return useQuery({
    queryKey: ["evaluations", "runs", query],
    queryFn: () => listEvaluationRuns(query),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => {
      const items = q.state.data?.items ?? [];
      return items.some((r) => ACTIVE_EVALUATION_STATUSES.has(r.status)) ? 5_000 : false;
    },
  });
}

export function useEvaluationRun(evaluationRunId: string) {
  return useQuery({
    queryKey: ["evaluations", "runs", evaluationRunId],
    queryFn: () => getEvaluationRun(evaluationRunId),
    enabled: Boolean(evaluationRunId),
    refetchInterval: (q) =>
      q.state.data && ACTIVE_EVALUATION_STATUSES.has(q.state.data.status) ? 3_000 : false,
  });
}

export function useEvaluationResults(evaluationRunId: string) {
  return useQuery({
    queryKey: ["evaluations", "runs", evaluationRunId, "results"],
    queryFn: () => listEvaluationResults(evaluationRunId),
    enabled: Boolean(evaluationRunId),
  });
}

export function useEvaluationMetrics(query: EvaluationMetricsQuery = {}) {
  return useQuery({
    queryKey: ["evaluations", "metrics", query],
    queryFn: () => getEvaluationMetrics(query),
  });
}

export function useCreateEvaluationRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: EvaluationRunCreateRequest) => createEvaluationRun(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}
