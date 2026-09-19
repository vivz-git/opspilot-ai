import { JsonBlock } from "@/components/json-block";
import { Separator } from "@/components/ui/separator";
import type { ApprovalResource } from "@/lib/api/client";
import { parseApprovalPreview } from "@/lib/approval-payload";
import { cn } from "@/lib/utils";

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  if (value == null || value === "") return null;
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="break-all font-mono text-sm">{value}</span>
    </div>
  );
}

function EmailBody({ body }: { body: string | null }) {
  if (!body) {
    return <p className="text-sm text-muted-foreground">No draft body was recorded.</p>;
  }
  return (
    <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-md border border-border/60 bg-background p-4 font-sans text-sm leading-relaxed text-foreground">
      {body}
    </pre>
  );
}

function CustomerDiffTable({ diff }: { diff: Record<string, { before: unknown; after: unknown }> }) {
  const fields = Object.keys(diff);
  if (fields.length === 0) {
    return <p className="text-sm text-muted-foreground">No field changes were recorded.</p>;
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-border/60 text-left text-xs uppercase tracking-wide text-muted-foreground">
          <th className="pb-2 pr-4 font-medium">Field</th>
          <th className="pb-2 pr-4 font-medium">Before</th>
          <th className="pb-2 font-medium">After</th>
        </tr>
      </thead>
      <tbody>
        {fields.map((field) => (
          <tr key={field} className="border-b border-border/40 last:border-0">
            <td className="py-2 pr-4 font-mono text-xs text-muted-foreground">{field}</td>
            <td className="py-2 pr-4 font-mono text-xs text-destructive/90">
              {JSON.stringify(diff[field]!.before)}
            </td>
            <td className="py-2 font-mono text-xs text-success">{JSON.stringify(diff[field]!.after)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The complete, de-referenced payload the operator is authorizing —
 * §5.2/§5.3 of docs/frontend-design.md's approvals contract: what will
 * happen, in full, not a summary. Renders the server's already-redacted
 * `payload_preview` exactly as returned; builds nothing itself.
 */
export function ApprovalPayloadPreview({ approval }: { approval: ApprovalResource }) {
  const parsed = parseApprovalPreview(approval);

  if (parsed.kind === "email") {
    return (
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Field label="To" value={parsed.to} />
          <Field label="Draft ID" value={parsed.draftId} />
          <Field label="Lead ID" value={parsed.leadId} />
        </div>
        {parsed.subject && (
          <div className="flex flex-col gap-0.5">
            <span className="text-xs uppercase tracking-wide text-muted-foreground">Subject</span>
            <span className="text-sm font-medium">{parsed.subject}</span>
          </div>
        )}
        <div className="flex flex-col gap-1">
          <span className="text-xs uppercase tracking-wide text-muted-foreground">
            Email body — exactly as it will be sent
          </span>
          <EmailBody body={parsed.body} />
        </div>
        <Separator />
        <JsonBlock label="Resolved arguments" value={parsed.args} />
      </div>
    );
  }

  if (parsed.kind === "customer_update") {
    return (
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Field label="Customer" value={parsed.accountName ?? parsed.customerId} />
          <Field label="Contact" value={parsed.primaryContact} />
          <Field label="Email" value={parsed.email} />
        </div>
        {parsed.versionMatch === false && (
          <p className="rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-sm text-warning">
            Version mismatch: this approval was built against version {parsed.expectedVersion}, but the
            customer record is now at version {parsed.currentVersion}. Approving may be rejected by the
            server as a stale write.
          </p>
        )}
        {parsed.reason && (
          <div className="flex flex-col gap-0.5">
            <span className="text-xs uppercase tracking-wide text-muted-foreground">
              Agent&apos;s stated reason
            </span>
            <span className="text-sm">{parsed.reason}</span>
          </div>
        )}
        <div className="flex flex-col gap-1">
          <span className="text-xs uppercase tracking-wide text-muted-foreground">Field changes</span>
          <CustomerDiffTable diff={parsed.diff} />
        </div>
        <Separator />
        <JsonBlock label="Resolved arguments" value={parsed.args} />
      </div>
    );
  }

  return (
    <div className={cn("flex flex-col gap-2")}>
      <p className="text-sm text-muted-foreground">
        No specialized preview for <span className="font-mono">{approval.tool}</span> — showing the
        resolved arguments in full.
      </p>
      <JsonBlock label="Resolved arguments" value={parsed.args ?? approval.payload_preview} />
    </div>
  );
}
