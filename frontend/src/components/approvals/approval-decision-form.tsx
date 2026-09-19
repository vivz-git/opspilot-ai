"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, type ApprovalDecisionKind, type ApprovalResource } from "@/lib/api/client";
import { useDecideApproval } from "@/lib/api/queries";
import { cn } from "@/lib/utils";

/**
 * The four conflict codes `decide_approval` can raise (all HTTP 409 —
 * `backend/app/api/errors.py`), each meaning something a generic "request
 * failed" message would hide. §5.5's "calm, not alarmed" applies here too:
 * these are informational, not siren-red panic states.
 */
const CONFLICT_MESSAGE: Record<string, string> = {
  approval_not_pending:
    "This approval was already decided. Reloading will show the recorded decision.",
  approval_expired:
    "This approval expired before a decision was recorded. It can no longer be approved or rejected.",
  approval_superseded:
    "This screen is stale — the authorized arguments changed since you loaded it. Reload to see the current approval before deciding.",
  run_not_resumable:
    "The run this approval belongs to is no longer in a state that can resume. The decision was not applied.",
};

function conflictMessageFor(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const code = (error.body as { code?: string } | undefined)?.code;
  if (code && CONFLICT_MESSAGE[code]) return CONFLICT_MESSAGE[code];
  return "This approval could not be decided because it is no longer in the state this screen expected.";
}

/**
 * Approve/Reject for one approval — §5.4/§5.9 of docs/frontend-design.md.
 * No local approval state machine: the only source of truth for whether
 * this approval is still decidable is `approval.status`, refetched by
 * `useDecideApproval`'s `onSettled` after every attempt (success or
 * conflict). `args_hash` is always the value already on `approval` as
 * fetched from the server — never recomputed here.
 */
export function ApprovalDecisionForm({ approval }: { approval: ApprovalResource }) {
  const [showReject, setShowReject] = useState(false);
  const [reason, setReason] = useState("");
  const [reasonTouched, setReasonTouched] = useState(false);
  const mutation = useDecideApproval(approval.approval_id);

  const isPending = approval.status === "pending";
  const reasonBlank = reason.trim().length === 0;
  const conflictMessage = conflictMessageFor(mutation.error);

  function submit(decision: ApprovalDecisionKind) {
    if (decision === "reject" && reasonBlank) {
      setReasonTouched(true);
      return;
    }
    mutation.mutate({
      decision,
      args_hash: approval.args_hash,
      ...(decision === "reject" ? { reason: reason.trim() } : {}),
    });
  }

  if (!isPending) {
    return (
      <p className="text-sm text-muted-foreground">
        This approval is <span className="font-mono">{approval.status}</span> and can no longer be
        decided from this screen.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {mutation.isError && (
        <p
          role="alert"
          className="rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-sm text-warning"
        >
          {conflictMessage ??
            (mutation.error instanceof Error ? mutation.error.message : "The decision could not be recorded.")}
        </p>
      )}

      {showReject && (
        <div className="flex flex-col gap-1.5">
          <label htmlFor="reject-reason" className="text-xs font-medium text-muted-foreground">
            Rejection reason <span className="text-destructive">(required)</span>
          </label>
          <Textarea
            id="reject-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            onBlur={() => setReasonTouched(true)}
            placeholder="Why is this being rejected?"
            aria-invalid={reasonTouched && reasonBlank}
            aria-describedby={reasonTouched && reasonBlank ? "reject-reason-error" : undefined}
            className={cn(reasonTouched && reasonBlank && "border-destructive focus-visible:ring-destructive")}
          />
          {reasonTouched && reasonBlank && (
            <p id="reject-reason-error" className="text-xs text-destructive">
              A reason is required to reject.
            </p>
          )}
        </div>
      )}

      <div className="flex gap-3">
        <Button
          size="lg"
          className="flex-1"
          onClick={() => submit("approve")}
          disabled={mutation.isPending}
        >
          Approve
        </Button>
        <Button
          variant="destructive"
          size="lg"
          className="flex-1"
          onClick={() => (showReject ? submit("reject") : setShowReject(true))}
          disabled={mutation.isPending || (showReject && reasonTouched && reasonBlank)}
        >
          {showReject ? "Confirm reject" : "Reject"}
        </Button>
      </div>
    </div>
  );
}
