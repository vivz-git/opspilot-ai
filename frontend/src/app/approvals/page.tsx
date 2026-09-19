"use client";

import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useApprovalQueue } from "@/lib/api/queries";

export default function ApprovalsPage() {
  const { data, isPending, isError, error } = useApprovalQueue();

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-lg font-semibold">Approvals</h1>
        <p className="text-sm text-muted-foreground">
          Mutating or outbound steps paused for a human decision, with the exact arguments that
          will be sent.
        </p>
      </div>

      {isPending && (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      )}

      {isError && (
        <Card>
          <CardHeader>
            <CardTitle>Could not load the approval queue</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {error instanceof Error ? error.message : "Unknown error"}. Is the OpsPilot API
            running at the configured base URL?
          </CardContent>
        </Card>
      )}

      {data && data.length === 0 && (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            Nothing pending. Risky steps will appear here the moment a run reaches one.
          </CardContent>
        </Card>
      )}

      {data && data.length > 0 && (
        <div className="flex flex-col gap-2">
          {data.map((approval) => (
            <Card key={approval.approval_id}>
              <CardContent className="flex flex-col gap-2 pt-6">
                <div className="flex items-center justify-between gap-4">
                  <span className="text-sm font-medium">{approval.title}</span>
                  <div className="flex shrink-0 items-center gap-2">
                    <Badge variant="outline" className="font-mono">
                      {approval.tool}
                    </Badge>
                    <StatusBadge status={approval.status} />
                  </div>
                </div>
                <p className="text-sm text-muted-foreground">{approval.summary}</p>
                <span className="font-mono text-xs text-muted-foreground">
                  run {approval.run_id} · step {approval.step_id}
                </span>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
