"use client";

import { StatusBadge } from "@/components/status-badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
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
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
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
        <div className="flex flex-col gap-2">
          {data.items.map((run) => (
            <Card key={run.run_id}>
              <CardContent className="flex items-center justify-between gap-4 pt-6">
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="truncate text-sm font-medium">{run.user_request}</span>
                  <span className="font-mono text-xs text-muted-foreground">{run.run_id}</span>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <span className="text-xs text-muted-foreground">
                    {run.counters.step_count} steps
                  </span>
                  <StatusBadge status={run.status} />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
