"use client";

import Link from "next/link";

import { ApprovalDecisionForm } from "@/components/approvals/approval-decision-form";
import { ApprovalPayloadPreview } from "@/components/approvals/approval-payload-preview";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";
import { useApproval } from "@/lib/api/queries";
import { formatTimestamp } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The one approve/reject surface (§5.6) — a run's `ApprovalPanel` and the
 * approvals queue both link here rather than deciding anything themselves.
 */
export function ApprovalDetailView({ approvalId }: { approvalId: string }) {
  const query = useApproval(approvalId);

  if (query.isPending) {
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (query.isError) {
    const notFound = query.error instanceof ApiError && query.error.status === 404;
    return (
      <Card>
        <CardHeader>
          <CardTitle>{notFound ? "Approval not found" : "Could not load this approval"}</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          {notFound
            ? `No approval exists with id ${approvalId}.`
            : query.error instanceof Error
              ? query.error.message
              : "Unknown error."}{" "}
          {!notFound && "Is the OpsPilot API running at the configured base URL?"}
        </CardContent>
      </Card>
    );
  }

  const approval = query.data;

  return (
    <div className="flex flex-col gap-6">
      <Card className={cn(approval.status === "pending" && "border-warning/40 bg-warning/5")}>
        <CardHeader className="flex-row items-start justify-between gap-4 space-y-0">
          <div>
            <div className="mb-2 flex items-center gap-2">
              <StatusBadge status={approval.status} />
              <Badge variant="outline" className="font-mono uppercase">
                {approval.risk}
              </Badge>
              <Badge variant="outline" className="font-mono">
                {approval.tool}
              </Badge>
            </div>
            <CardTitle className="text-lg">{approval.title}</CardTitle>
            <p className="mt-1 text-sm text-muted-foreground">{approval.summary}</p>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-x-8 gap-y-2 text-xs">
            <div className="flex flex-col gap-0.5">
              <span className="uppercase tracking-wide text-muted-foreground">Run / step</span>
              <Link
                href={`/runs/${approval.run_id}`}
                className="font-mono text-foreground underline-offset-2 hover:underline"
              >
                {approval.run_id} · {approval.step_id}
              </Link>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="uppercase tracking-wide text-muted-foreground">Requested</span>
              <span className="font-mono text-foreground">{formatTimestamp(approval.requested_at)}</span>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="uppercase tracking-wide text-muted-foreground">Expires</span>
              <span className="font-mono text-foreground">{formatTimestamp(approval.expires_at)}</span>
            </div>
            {approval.decided_at && (
              <div className="flex flex-col gap-0.5">
                <span className="uppercase tracking-wide text-muted-foreground">Decided</span>
                <span className="font-mono text-foreground">
                  {formatTimestamp(approval.decided_at)}
                  {approval.decided_by ? ` · ${approval.decided_by}` : ""}
                </span>
              </div>
            )}
            <div className="flex flex-col gap-0.5">
              <span className="uppercase tracking-wide text-muted-foreground">Args hash</span>
              <span className="break-all font-mono text-foreground">{approval.args_hash}</span>
            </div>
          </div>
          {approval.reason && (
            <div className="flex flex-col gap-0.5">
              <span className="text-xs uppercase tracking-wide text-muted-foreground">
                Decision reason
              </span>
              <span className="text-sm">{approval.reason}</span>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">What will happen</CardTitle>
          <p className="text-sm text-muted-foreground">
            This is the exact, authorized payload this tool call will run with — not a summary.
          </p>
        </CardHeader>
        <CardContent>
          <ApprovalPayloadPreview approval={approval} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Decision</CardTitle>
        </CardHeader>
        <CardContent>
          <Separator className="mb-4" />
          <ApprovalDecisionForm approval={approval} />
        </CardContent>
      </Card>
    </div>
  );
}
