"use client";

import { useRouter } from "next/navigation";

import { EvaluationMetricsPanel } from "@/components/evaluations/evaluation-metrics-panel";
import { EvaluationRunsTable } from "@/components/evaluations/evaluation-runs-table";
import { RunSuiteControl } from "@/components/evaluations/run-suite-control";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { EvaluationRunResource } from "@/lib/api/client";
import { useEvaluationMetrics, useEvaluationRuns } from "@/lib/api/queries";

export default function EvaluationsPage() {
  const router = useRouter();
  const runsQuery = useEvaluationRuns({ limit: 50 });
  const metricsQuery = useEvaluationMetrics();

  function handleSelectRun(run: EvaluationRunResource) {
    router.push(`/evaluations/${run.evaluation_run_id}`);
  }

  function handleStarted(evaluationRunId: string) {
    router.push(`/evaluations/${evaluationRunId}`);
  }

  const items = runsQuery.data?.items ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold">Evaluations</h1>
          <p className="text-sm text-muted-foreground">
            Suite runs against the real service path, with pass/fail and invariant evidence from
            the server — never recomputed here.
          </p>
        </div>
        <RunSuiteControl onStarted={handleStarted} />
      </div>

      {metricsQuery.data && <EvaluationMetricsPanel metrics={metricsQuery.data.metrics} />}

      {runsQuery.isPending && (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      )}

      {runsQuery.isError && (
        <Card>
          <CardHeader>
            <CardTitle>Could not load evaluation runs</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {runsQuery.error instanceof Error ? runsQuery.error.message : "Unknown error"}. Is the
            OpsPilot API running at the configured base URL?
          </CardContent>
        </Card>
      )}

      {!runsQuery.isPending && !runsQuery.isError && items.length === 0 && (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            No evaluation runs yet. Start one above, or from{" "}
            <code className="font-mono text-xs">python -m app.evaluation.cli run</code>.
          </CardContent>
        </Card>
      )}

      {!runsQuery.isPending && !runsQuery.isError && items.length > 0 && (
        <Card className="overflow-hidden p-0">
          <EvaluationRunsTable runs={items} onSelectRun={handleSelectRun} />
        </Card>
      )}
    </div>
  );
}
