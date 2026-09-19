"use client";

import * as React from "react";

import { ApprovalPanel } from "@/components/run-detail/approval-panel";
import { ExecutionTimeline } from "@/components/run-detail/execution-timeline";
import { PlanVsActual } from "@/components/run-detail/plan-vs-actual";
import { RunHeader } from "@/components/run-detail/run-header";
import { StepInspector } from "@/components/run-detail/step-inspector";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";
import { useRun, useRunTrace } from "@/lib/api/queries";

export function RunDetailView({ runId }: { runId: string }) {
  const [selectedStepId, setSelectedStepId] = React.useState<string | null>(null);

  const runQuery = useRun(runId);
  const traceQuery = useRunTrace(runId);

  if (runQuery.isPending || traceQuery.isPending) {
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-64 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (runQuery.isError) {
    const notFound = runQuery.error instanceof ApiError && runQuery.error.status === 404;
    return (
      <Card>
        <CardHeader>
          <CardTitle>{notFound ? "Run not found" : "Could not load this run"}</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          {notFound
            ? `No run exists with id ${runId}.`
            : runQuery.error instanceof Error
              ? runQuery.error.message
              : "Unknown error."}{" "}
          {!notFound && "Is the OpsPilot API running at the configured base URL?"}
        </CardContent>
      </Card>
    );
  }

  if (traceQuery.isError) {
    return (
      <div className="flex flex-col gap-6">
        <RunHeader run={runQuery.data} />
        <Card>
          <CardHeader>
            <CardTitle>Could not load the execution trace</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {traceQuery.error instanceof Error ? traceQuery.error.message : "Unknown error."}
          </CardContent>
        </Card>
      </div>
    );
  }

  const run = runQuery.data;
  const events = traceQuery.data;

  const selectedActual =
    selectedStepId != null ? (run.steps ?? []).find((s) => s.step_id === selectedStepId) ?? null : null;

  return (
    <div className="flex flex-col gap-6">
      <RunHeader run={run} />
      {run.pending_approval && <ApprovalPanel approval={run.pending_approval} />}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="flex flex-col gap-6 lg:col-span-2">
          <PlanVsActual run={run} selectedStepId={selectedStepId} onSelectStep={setSelectedStepId} />
          <ExecutionTimeline events={events} selectedStepId={selectedStepId} onSelectStep={setSelectedStepId} />
        </div>
        <div className="lg:col-span-1">
          {selectedStepId ? (
            <StepInspector
              stepId={selectedStepId}
              actual={selectedActual}
              events={events}
              onClose={() => setSelectedStepId(null)}
            />
          ) : (
            <Card className="sticky top-4">
              <CardContent className="pt-6 text-sm text-muted-foreground">
                Select a step from the plan or the timeline to inspect its input, output and verification
                checks.
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
