"use client";

import { X } from "lucide-react";

import { JsonBlock } from "@/components/json-block";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import type { RunStepSummary, TraceEventResource } from "@/lib/api/client";
import { formatDuration, formatTimestamp } from "@/lib/format";
import { eventsForStep, groupStepBlocks, type AttemptBlock } from "@/lib/trace";

interface VerificationCheck {
  name: string;
  passed: boolean;
  expected?: unknown;
  observed?: unknown;
}

function VerificationChecks({ event }: { event: TraceEventResource }) {
  const payload = event.payload as { checks?: unknown; detail?: unknown } | undefined;
  const checks = Array.isArray(payload?.checks) ? (payload.checks as VerificationCheck[]) : [];
  const detail = typeof payload?.detail === "string" ? payload.detail : null;

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border/60 p-2">
      <div className="flex items-center gap-2">
        <StatusBadge status={event.status ?? event.kind} />
        <span className="text-xs text-muted-foreground">{formatTimestamp(event.ts)}</span>
        {event.duration_ms != null && (
          <span className="font-mono text-xs text-muted-foreground">{formatDuration(event.duration_ms)}</span>
        )}
      </div>
      {detail && <p className="text-xs text-muted-foreground">{detail}</p>}
      {checks.length > 0 && (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="pb-1 pr-2 font-medium">Check</th>
              <th className="pb-1 pr-2 font-medium">Expected</th>
              <th className="pb-1 pr-2 font-medium">Observed</th>
              <th className="pb-1 font-medium">Result</th>
            </tr>
          </thead>
          <tbody>
            {checks.map((check, i) => (
              <tr key={`${check.name}-${i}`} className="border-t border-border/40">
                <td className="py-1 pr-2 font-mono">{check.name}</td>
                <td className="py-1 pr-2 font-mono text-muted-foreground">{JSON.stringify(check.expected)}</td>
                <td className="py-1 pr-2 font-mono text-muted-foreground">{JSON.stringify(check.observed)}</td>
                <td className="py-1">
                  <span className={check.passed ? "text-success" : "text-destructive"}>
                    {check.passed ? "pass" : "fail"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function AttemptDetail({ block }: { block: AttemptBlock }) {
  const errorPayload = block.closed?.error;

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border/60 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-semibold">Attempt {block.attempt}</span>
        {block.closed?.status && <StatusBadge status={block.closed.status} />}
        {block.closed?.duration_ms != null && (
          <span className="font-mono text-xs text-muted-foreground">{formatDuration(block.closed.duration_ms)}</span>
        )}
        {block.started && (
          <span className="text-xs text-muted-foreground">started {formatTimestamp(block.started.ts)}</span>
        )}
      </div>
      <JsonBlock label="Input" value={block.started?.input} />
      <JsonBlock label="Output" value={block.closed?.output} />
      {errorPayload && <JsonBlock label="Error" value={errorPayload} />}
      {block.verifications.map((v) => (
        <VerificationChecks key={v.seq} event={v} />
      ))}
    </div>
  );
}

export function StepInspector({
  stepId,
  actual,
  events,
  onClose,
}: {
  stepId: string;
  actual: RunStepSummary | null;
  events: TraceEventResource[];
  onClose: () => void;
}) {
  const stepEvents = eventsForStep(events, stepId);
  const blocks = groupStepBlocks(stepEvents).filter((b): b is AttemptBlock => b.kind === "attempt");

  return (
    <Card className="sticky top-4">
      <CardHeader className="flex-row items-start justify-between space-y-0">
        <div className="flex flex-col gap-1">
          <CardTitle className="font-mono text-sm">{stepId}</CardTitle>
          {actual && (
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline" className="font-mono text-[10px]">
                {actual.tool}
              </Badge>
              <StatusBadge status={actual.status} />
            </div>
          )}
        </div>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close step inspector">
          <X className="h-4 w-4" />
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {actual && (
          <div className="grid grid-cols-3 gap-3 text-xs">
            <div>
              <div className="text-muted-foreground">Attempts</div>
              <div className="font-mono">{actual.attempts}</div>
            </div>
            <div>
              <div className="text-muted-foreground">Retries</div>
              <div className="font-mono">{actual.retry_count}</div>
            </div>
            <div>
              <div className="text-muted-foreground">Duration</div>
              <div className="font-mono">{formatDuration(actual.duration_ms)}</div>
            </div>
          </div>
        )}
        {actual?.error && (
          <>
            <Separator />
            <JsonBlock label="Last recorded error" value={actual.error} />
          </>
        )}

        <Separator />

        {blocks.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {actual ? "No per-attempt trace events recorded." : "This step has not run yet — no trace events."}
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            {blocks.map((block) => (
              <AttemptDetail key={block.attempt} block={block} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
