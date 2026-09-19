"use client";

import { useQuery } from "@tanstack/react-query";

import { getHealthz, getRun, listApprovalQueue, listRuns } from "./client";

export function useHealthz() {
  return useQuery({
    queryKey: ["healthz"],
    queryFn: getHealthz,
    refetchInterval: 30_000,
  });
}

export function useRuns() {
  return useQuery({
    queryKey: ["runs"],
    queryFn: listRuns,
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
