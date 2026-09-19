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
import type { EvaluationResultResource } from "@/lib/api/client";
import { formatDuration } from "@/lib/format";

export function EvaluationCaseTable({
  results,
  selectedCaseId,
  onSelectCase,
}: {
  results: EvaluationResultResource[];
  selectedCaseId: string | null;
  onSelectCase: (result: EvaluationResultResource) => void;
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Case</TableHead>
          <TableHead className="text-right">Duration</TableHead>
          <TableHead className="text-right">Retries</TableHead>
          <TableHead className="text-right">Tool calls</TableHead>
          <TableHead>Approval</TableHead>
          <TableHead>Result</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {results.map((result) => (
          <TableRow
            key={result.result_id}
            role="link"
            tabIndex={0}
            data-state={selectedCaseId === result.case_id ? "selected" : undefined}
            aria-label={`Inspect case ${result.case_id}`}
            aria-current={selectedCaseId === result.case_id ? "true" : undefined}
            className="cursor-pointer"
            onClick={() => onSelectCase(result)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelectCase(result);
              }
            }}
          >
            <TableCell className="font-mono text-sm font-medium">{result.case_id}</TableCell>
            <TableCell className="whitespace-nowrap text-right font-mono text-xs">
              {formatDuration(result.duration_ms)}
            </TableCell>
            <TableCell className="text-right font-mono text-xs">{result.retry_count}</TableCell>
            <TableCell className="text-right font-mono text-xs">
              {result.tool_calls_count}
            </TableCell>
            <TableCell className="font-mono text-xs text-muted-foreground">
              {result.approval_outcome ?? "—"}
            </TableCell>
            <TableCell>
              <StatusBadge status={result.passed ? "passed" : "failed"} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
