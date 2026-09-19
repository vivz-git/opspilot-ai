"use client";

import { StatusBadge } from "@/components/status-badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useRuns } from "@/lib/api/queries";

export default function RunsPage() {
  const { data, isPending, isError, error } = useRuns();

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-lg font-semibold">Runs</h1>
        <p className="text-sm text-muted-foreground">
          Every request submitted to the agent, as a planned, budgeted, terminating unit of work.
        </p>
      </div>

      {isPending && (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      )}

      {isError && (
        <Card>
          <CardHeader>
            <CardTitle>Could not load runs</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {error instanceof Error ? error.message : "Unknown error"}. Is the OpsPilot API
            running at the configured base URL?
          </CardContent>
        </Card>
      )}

      {data && data.items.length === 0 && (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            No runs yet. Submit a request through the OpsPilot API to see it here.
          </CardContent>
        </Card>
      )}

      {data && data.items.length > 0 && (
        <Card className="overflow-hidden p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Request</TableHead>
                <TableHead>Run ID</TableHead>
                <TableHead className="text-right">Steps</TableHead>
                <TableHead className="text-right">Retries</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((run) => (
                <TableRow key={run.run_id}>
                  <TableCell className="max-w-xs truncate font-medium">
                    {run.user_request}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {run.run_id}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {run.counters.step_count}
                  </TableCell>
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
        </Card>
      )}
    </div>
  );
}
