"use client";

import { StatusBadge } from "@/components/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { EvaluationRunResource } from "@/lib/api/client";
import { formatCount, formatRate } from "@/lib/evaluations";
import { formatDuration, formatTimestamp } from "@/lib/format";

function durationMs(run: EvaluationRunResource): number | null {
  if (!run.finished_at) return null;
  const started = new Date(run.started_at).getTime();
  const finished = new Date(run.finished_at).getTime();
  if (Number.isNaN(started) || Number.isNaN(finished)) return null;
  return Math.max(0, finished - started);
}

export function EvaluationRunsTable({
  runs,
  onSelectRun,
}: {
  runs: EvaluationRunResource[];
  onSelectRun: (run: EvaluationRunResource) => void;
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Suite</TableHead>
          <TableHead>Started</TableHead>
          <TableHead className="text-right">Duration</TableHead>
          <TableHead className="text-right">Cases</TableHead>
          <TableHead className="text-right">Passed</TableHead>
          <TableHead className="text-right">Failed</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {runs.map((run) => (
          <TableRow
            key={run.evaluation_run_id}
            role="link"
            tabIndex={0}
            aria-label={`Open evaluation run ${run.evaluation_run_id}`}
            className="cursor-pointer"
            onClick={() => onSelectRun(run)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelectRun(run);
              }
            }}
          >
            <TableCell className="font-mono text-sm font-medium">{run.suite}</TableCell>
            <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
              {formatTimestamp(run.started_at)}
            </TableCell>
            <TableCell className="whitespace-nowrap text-right font-mono text-xs">
              {run.status === "running" ? (
                <span className="text-info">running…</span>
              ) : (
                formatDuration(durationMs(run))
              )}
            </TableCell>
            <TableCell className="text-right font-mono text-xs">
              {formatCount(run.case_count)}
            </TableCell>
            <TableCell className="text-right font-mono text-xs text-success">
              {formatCount(run.passed)}
            </TableCell>
            <TableCell className="text-right font-mono text-xs">
              <span className={run.failed > 0 ? "text-destructive" : "text-muted-foreground"}>
                {formatCount(run.failed)}
              </span>
            </TableCell>
            <TableCell>
              <div className="flex items-center gap-2">
                <StatusBadge status={run.status} />
                {run.case_count > 0 && (
                  <span className="font-mono text-xs text-muted-foreground">
                    {formatRate(run.passed / run.case_count)}
                  </span>
                )}
              </div>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
