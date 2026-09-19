"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { getHealthz, getRun, listApprovalQueue, listRuns, type RunListQuery } from "./client";

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

export function useApprovalQueue() {
  return useQuery({
    queryKey: ["approvals", "queue"],
    queryFn: listApprovalQueue,
    refetchInterval: 15_000,
  });
}
