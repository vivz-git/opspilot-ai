"use client";

import { StatusBadge, isLiveStatus } from "@/components/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { RunSummary } from "@/lib/api/client";
import { formatDuration, formatTimestamp } from "@/lib/format";

export function RunsTable({
  runs,
  onSelectRun,
}: {
  runs: RunSummary[];
  onSelectRun: (run: RunSummary) => void;
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Request</TableHead>
          <TableHead>Run ID</TableHead>
          <TableHead>Created</TableHead>
          <TableHead className="text-right">Duration</TableHead>
          <TableHead className="text-right">Steps</TableHead>
          <TableHead className="text-right">Retries</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {runs.map((run) => (
          <TableRow
            key={run.run_id}
            role="link"
            tabIndex={0}
            title={JSON.stringify({ parent_run_id: run.parent_run_id, ...run.metadata })}
            aria-label={`Open run ${run.run_id}`}
            className="cursor-pointer"
            onClick={() => onSelectRun(run)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelectRun(run);
              }
            }}
          >
            <TableCell className="max-w-xs truncate font-medium">{run.user_request}</TableCell>
            <TableCell className="font-mono text-xs text-muted-foreground">{run.run_id}</TableCell>
            <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
              {formatTimestamp(run.timestamps.created_at)}
            </TableCell>
            <TableCell className="whitespace-nowrap text-right font-mono text-xs">
              {run.duration_ms == null && isLiveStatus(run.status) ? (
                <span className="text-info">running…</span>
              ) : (
                formatDuration(run.duration_ms)
              )}
            </TableCell>
            <TableCell className="text-right font-mono text-xs">{run.counters.step_count}</TableCell>
            <TableCell className="text-right font-mono text-xs">
              {run.counters.retry_total}
            </TableCell>
            <TableCell>
              <StatusBadge status={run.status} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
