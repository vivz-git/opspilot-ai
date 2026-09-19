"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { JsonBlock } from "@/components/ui/json-block";
import { Separator } from "@/components/ui/separator";
import { ApiError } from "@/lib/api/client";
import { useApproval, useDecideApproval } from "@/lib/api/queries";
import { describeDecisionError } from "@/lib/approvals";
import { formatTimestamp } from "@/lib/format";

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-muted-foreground">{label}</div>
      <div className="font-mono">{value}</div>
    </div>
  );
}

/**
 * The full record for one approval (§13.5) plus the decision action itself
 * — the part `ApprovalPanel` (in run detail) deliberately defers here. Both
 * mutating paths (`approve`, `reject`) go through the same
 * `POST /approvals/{id}/decision` the backend gate re-verifies against the
 * stored args_hash; nothing here computes or asserts authorization itself.
 */
export function ApprovalDetailView({ approvalId }: { approvalId: string }) {
  const [rejecting, setRejecting] = React.useState(false);
  const [reason, setReason] = React.useState("");

  const approvalQuery = useApproval(approvalId);
  const decide = useDecideApproval(approvalId);

  if (approvalQuery.isPending) {
    return <div className="h-48 animate-pulse rounded-lg bg-muted" />;
  }

  if (approvalQuery.isError) {
    const notFound = approvalQuery.error instanceof ApiError && approvalQuery.error.status === 404;
    return (
      <Card>
        <CardHeader>
          <CardTitle>{notFound ? "Approval not found" : "Could not load this approval"}</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          {notFound
            ? `No approval exists with id ${approvalId}.`
            : approvalQuery.error instanceof Error
              ? approvalQuery.error.message
              : "Unknown error."}
        </CardContent>
      </Card>
    );
  }

  const approval = approvalQuery.data;
  const isPending = approval.status === "pending";

  function handleApprove() {
    decide.mutate({ decision: "approve", args_hash: approval.args_hash });
  }

  function handleConfirmReject() {
    if (!reason.trim()) return;
    decide.mutate({ decision: "reject", args_hash: approval.args_hash, reason: reason.trim() });
  }

  return (
    <div className="flex flex-col gap-6">
      <Link
        href="/approvals"
        className="flex w-fit items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
        Back to approvals
      </Link>

      <Card>
        <CardHeader className="flex-row flex-wrap items-start justify-between gap-2 space-y-0">
          <div className="flex flex-col gap-1">
            <CardTitle className="text-base">{approval.title}</CardTitle>
            <p className="text-sm text-muted-foreground">{approval.summary}</p>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="font-mono uppercase">
              {approval.risk}
            </Badge>
            <StatusBadge status={approval.status} />
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
            <Fact label="Tool" value={approval.tool} />
            <Fact label="Step" value={approval.step_id} />
            <Fact
              label="Run"
              value={
                <Link href={`/runs/${approval.run_id}`} className="text-primary hover:underline">
                  {approval.run_id}
                </Link>
              }
            />
            <Fact label="Requested" value={formatTimestamp(approval.requested_at)} />
            <Fact label="Expires" value={formatTimestamp(approval.expires_at)} />
            {approval.decided_at && (
              <Fact label="Decided" value={formatTimestamp(approval.decided_at)} />
            )}
            {approval.decided_by && <Fact label="Decided by" value={approval.decided_by} />}
          </div>

          {approval.reason && (
            <div className="rounded-md border border-border/60 p-2 text-xs">
              <span className="text-muted-foreground">Reason: </span>
              {approval.reason}
            </div>
          )}

          <Separator />

          <JsonBlock label="Payload preview" value={approval.payload_preview} copyable />
          <div className="text-xs text-muted-foreground">
            args_hash <span className="font-mono">{approval.args_hash}</span>
          </div>

          {isPending && (
            <>
              <Separator />
              {decide.isError && (
                <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
                  {describeDecisionError(decide.error)}
                </div>
              )}
              {!rejecting ? (
                <div className="flex gap-2">
                  <Button onClick={handleApprove} disabled={decide.isPending}>
                    {decide.isPending && decide.variables?.decision === "approve"
                      ? "Approving…"
                      : "Approve"}
                  </Button>
                  <Button
                    variant="destructive"
                    onClick={() => setRejecting(true)}
                    disabled={decide.isPending}
                  >
                    Reject
                  </Button>
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  <label htmlFor="reject-reason" className="text-xs font-medium text-muted-foreground">
                    Reason for rejecting (required)
                  </label>
                  <textarea
                    id="reject-reason"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    rows={3}
                    className="rounded-md border border-input bg-background p-2 text-sm"
                    placeholder="Why is this being rejected?"
                  />
                  <div className="flex gap-2">
                    <Button
                      variant="destructive"
                      onClick={handleConfirmReject}
                      disabled={decide.isPending || !reason.trim()}
                    >
                      {decide.isPending && decide.variables?.decision === "reject"
                        ? "Rejecting…"
                        : "Confirm reject"}
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() => {
                        setRejecting(false);
                        setReason("");
                      }}
                      disabled={decide.isPending}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
