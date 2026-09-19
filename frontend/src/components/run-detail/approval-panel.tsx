import Link from "next/link";
import { ArrowUpRight, ShieldAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { RunPendingApproval } from "@/lib/api/client";
import { formatTimestamp } from "@/lib/format";

/**
 * Renders the pending approval already carried on `RunResource.pending_approval`.
 * No approve/reject action here — the one decision surface is the approval
 * detail page this links to (§5.6: never a shortcut action bolted onto a
 * different page).
 */
export function ApprovalPanel({ approval }: { approval: RunPendingApproval }) {
  return (
    <Card className="border-warning/40 bg-warning/5">
      <CardHeader className="flex-row items-center gap-2 space-y-0">
        <ShieldAlert className="h-4 w-4 text-warning" aria-hidden />
        <CardTitle className="text-base text-warning">Awaiting approval</CardTitle>
        <Badge variant="outline" className="ml-auto font-mono uppercase">
          {approval.risk}
        </Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div>
          <p className="text-sm font-medium">{approval.title}</p>
          <p className="text-sm text-muted-foreground">{approval.summary}</p>
        </div>
        <div className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-xs text-muted-foreground">
          <span>tool={approval.tool}</span>
          <span>step={approval.step_id}</span>
          <span>expires {formatTimestamp(approval.expires_at)}</span>
        </div>
        <Button asChild variant="outline" size="sm" className="w-fit">
          <Link href={`/approvals/${approval.approval_id}`}>
            Review this approval
            <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
          </Link>
        </Button>
      </CardContent>
    </Card>
  );
}
