"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  decideApproval,
  getApproval,
  getHealthz,
  getRun,
  getRunTrace,
  listApprovalQueue,
  listRuns,
  type ApprovalDecisionRequest,
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

/**
 * The only state this drives is `ApprovalResource.status`, always freshly
 * read from the server — there is no local approval state machine. Every
 * settle (success *or* conflict) refetches this approval so the screen
 * shows the server's current truth (e.g. now "superseded" or "expired")
 * instead of the stale "pending" it was rendered from. It never fetches or
 * navigates to a *different* approval — the API doesn't expose which one
 * superseded this one, and even if it did, switching underneath the
 * operator without them asking is exactly what "never silently switch to
 * a newer approval" rules out.
 */
export function useDecideApproval(approvalId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ApprovalDecisionRequest) => decideApproval(approvalId, body),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["approvals", approvalId] });
      queryClient.invalidateQueries({ queryKey: ["approvals", "queue"] });
    },
  });
}
