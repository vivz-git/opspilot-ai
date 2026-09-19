"use client";

import { X } from "lucide-react";

import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import type { EvaluationResultResource } from "@/lib/api/client";
import { parseAssertions } from "@/lib/evaluations";
import { formatDuration } from "@/lib/format";

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  if (value == null) return null;
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <pre className="max-h-48 overflow-auto rounded-md border border-border/60 bg-background p-2 font-mono text-[11px] leading-relaxed">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

/**
 * The evidence for one case's pass/fail (§15.3 assertions, §15.6
 * invariants) — the server's own verdicts, only rendered here, never
 * recomputed. A failed assertion or violated invariant is surfaced first so
 * an operator scanning a failing suite does not have to hunt for it.
 */
export function CaseInspector({
  result,
  onClose,
}: {
  result: EvaluationResultResource;
  onClose: () => void;
}) {
  const assertions = parseAssertions(result.assertions);
  const failures = assertions.filter((a) => !a.passed);
  const passes = assertions.filter((a) => a.passed);

  return (
    <Card className="sticky top-4">
      <CardHeader className="flex-row items-start justify-between space-y-0">
        <div className="flex flex-col gap-1">
          <CardTitle className="font-mono text-sm">{result.case_id}</CardTitle>
          <StatusBadge status={result.passed ? "passed" : "failed"} />
        </div>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close case inspector">
          <X className="h-4 w-4" />
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid grid-cols-3 gap-3 text-xs">
          <div>
            <div className="text-muted-foreground">Duration</div>
            <div className="font-mono">{formatDuration(result.duration_ms)}</div>
          </div>
          <div>
            <div className="text-muted-foreground">Retries</div>
            <div className="font-mono">{result.retry_count}</div>
          </div>
          <div>
            <div className="text-muted-foreground">Tool calls</div>
            <div className="font-mono">{result.tool_calls_count}</div>
          </div>
        </div>

        {result.failure_reason && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
            {result.failure_reason}
          </div>
        )}

        <Separator />

        {failures.length > 0 && (
          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium text-destructive">
              Failed assertions ({failures.length})
            </span>
            {failures.map((a, i) => (
              <div
                key={`${a.name}-${i}`}
                className="flex flex-col gap-1 rounded-md border border-destructive/30 p-2"
              >
                <div className="flex items-center gap-2 font-mono text-xs">
                  {a.invariant && (
                    <span className="rounded bg-destructive/20 px-1 text-[10px] uppercase text-destructive">
                      invariant {a.invariant}
                    </span>
                  )}
                  {a.name}
                </div>
                {a.detail && <p className="text-xs text-muted-foreground">{a.detail}</p>}
                {a.evidence && <JsonBlock label="Evidence" value={a.evidence} />}
              </div>
            ))}
          </div>
        )}

        {assertions.length === 0 ? (
          <p className="text-sm text-muted-foreground">No assertion evidence recorded.</p>
        ) : (
          <details className="text-xs">
            <summary className="cursor-pointer select-none text-muted-foreground">
              {passes.length} passing assertion{passes.length === 1 ? "" : "s"}
            </summary>
            <ul className="mt-2 flex flex-col gap-1 font-mono">
              {passes.map((a, i) => (
                <li key={`${a.name}-${i}`} className="text-success">
                  {a.name}
                </li>
              ))}
            </ul>
          </details>
        )}
      </CardContent>
    </Card>
  );
}
