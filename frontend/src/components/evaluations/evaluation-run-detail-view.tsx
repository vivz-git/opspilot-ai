"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { CaseInspector } from "@/components/evaluations/case-inspector";
import { EvaluationCaseTable } from "@/components/evaluations/evaluation-case-table";
import { EvaluationMetricsPanel } from "@/components/evaluations/evaluation-metrics-panel";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { EvaluationResultResource } from "@/lib/api/client";
import { ApiError } from "@/lib/api/client";
import { useEvaluationResults, useEvaluationRun } from "@/lib/api/queries";
import { formatTimestamp } from "@/lib/format";

export function EvaluationRunDetailView({ evaluationRunId }: { evaluationRunId: string }) {
  const [selectedCaseId, setSelectedCaseId] = React.useState<string | null>(null);

  const runQuery = useEvaluationRun(evaluationRunId);
  const resultsQuery = useEvaluationResults(evaluationRunId);

  if (runQuery.isPending) {
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (runQuery.isError) {
    const notFound = runQuery.error instanceof ApiError && runQuery.error.status === 404;
    return (
      <Card>
        <CardHeader>
          <CardTitle>{notFound ? "Evaluation run not found" : "Could not load this evaluation run"}</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          {notFound
            ? `No evaluation run exists with id ${evaluationRunId}.`
            : runQuery.error instanceof Error
              ? runQuery.error.message
              : "Unknown error."}
        </CardContent>
      </Card>
    );
  }

  const run = runQuery.data;
  const results = resultsQuery.data ?? [];
  const selected = results.find((r) => r.case_id === selectedCaseId) ?? null;

  function handleSelectCase(result: EvaluationResultResource) {
    setSelectedCaseId((current) => (current === result.case_id ? null : result.case_id));
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <Link
          href="/evaluations"
          className="flex w-fit items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          Back to evaluations
        </Link>
        <Card>
          <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
            <div className="flex flex-col gap-1">
              <CardTitle className="font-mono text-base">{run.suite}</CardTitle>
              <span className="font-mono text-xs text-muted-foreground">
                {run.evaluation_run_id}
              </span>
            </div>
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <span>started {formatTimestamp(run.started_at)}</span>
              <StatusBadge status={run.status} />
            </div>
          </CardHeader>
        </Card>
      </div>

      <EvaluationMetricsPanel metrics={run.metrics} title="Run metrics" />

      {resultsQuery.isError && (
        <Card>
          <CardHeader>
            <CardTitle>Could not load case results</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {resultsQuery.error instanceof Error ? resultsQuery.error.message : "Unknown error."}
          </CardContent>
        </Card>
      )}

      {resultsQuery.isPending && <Skeleton className="h-48 w-full" />}

      {!resultsQuery.isPending && !resultsQuery.isError && results.length === 0 && (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            {run.status === "running"
              ? "This suite is still running — case results will appear as they complete."
              : "No case results were recorded for this run."}
          </CardContent>
        </Card>
      )}

      {results.length > 0 && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <Card className="overflow-hidden p-0">
              <EvaluationCaseTable
                results={results}
                selectedCaseId={selectedCaseId}
                onSelectCase={handleSelectCase}
              />
            </Card>
          </div>
          <div className="lg:col-span-1">
            {selected ? (
              <CaseInspector result={selected} onClose={() => setSelectedCaseId(null)} />
            ) : (
              <Card className="sticky top-4">
                <CardContent className="pt-6 text-sm text-muted-foreground">
                  Select a case to inspect its assertions and invariant evidence.
                </CardContent>
              </Card>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
