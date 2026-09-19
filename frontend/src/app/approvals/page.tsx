"use client";

import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
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
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
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
        <Card className="overflow-hidden p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Step</TableHead>
                <TableHead>Tool</TableHead>
                <TableHead>Run / step</TableHead>
                <TableHead>Risk</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((approval) => (
                <TableRow key={approval.approval_id}>
                  <TableCell className="max-w-sm">
                    <div className="font-medium">{approval.title}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {approval.summary}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline" className="font-mono">
                      {approval.tool}
                    </Badge>
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {approval.run_id} · {approval.step_id}
                  </TableCell>
                  <TableCell className="font-mono text-xs uppercase text-muted-foreground">
                    {approval.risk}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={approval.status} />
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
