import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { asMetricsSnapshot, formatCount, formatRate } from "@/lib/evaluations";
import { formatDuration } from "@/lib/format";

function MetricTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="font-mono text-lg font-semibold">{value}</span>
    </div>
  );
}

/**
 * Evidence-oriented metric readout (§15.4) — plain numbers, no decorative
 * charting. Renders `evaluation_runs.metrics` (or GET /evaluations/metrics)
 * verbatim; it never derives a rate or a percentile itself.
 */
export function EvaluationMetricsPanel({
  metrics,
  title = "Metrics",
}: {
  metrics: Record<string, unknown> | undefined;
  title?: string;
}) {
  const m = asMetricsSnapshot(metrics ?? {});
  const hasData = Object.keys(metrics ?? {}).length > 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {hasData ? (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
            <MetricTile label="Case pass rate" value={formatRate(m.case_pass_rate)} />
            <MetricTile label="Task success rate" value={formatRate(m.task_success_rate)} />
            <MetricTile label="Cases" value={formatCount(m.total_cases)} />
            <MetricTile label="Avg duration" value={formatDuration(m.avg_duration_ms)} />
            <MetricTile label="p50 duration" value={formatDuration(m.p50_duration_ms)} />
            <MetricTile label="p95 duration" value={formatDuration(m.p95_duration_ms)} />
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No evaluation runs recorded yet.</p>
        )}
      </CardContent>
    </Card>
  );
}
