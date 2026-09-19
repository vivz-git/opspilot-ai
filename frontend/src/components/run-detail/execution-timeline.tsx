"use client";

import { ArrowRight, CircleDot, Pause } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { TraceEventResource } from "@/lib/api/client";
import { formatDuration, formatTimestamp } from "@/lib/format";
import { buildTimeline, type AttemptBlock, type EventBlock, type TimelineEntry } from "@/lib/trace";
import { cn } from "@/lib/utils";

const SEVERITY_DOT: Record<TraceEventResource["severity"], string> = {
  debug: "bg-muted-foreground",
  info: "bg-info",
  warning: "bg-warning",
  error: "bg-destructive",
};

function MarkerRow({ event }: { event: TraceEventResource }) {
  return (
    <div className="flex items-center gap-3 py-1.5 pl-1 text-xs">
      <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", SEVERITY_DOT[event.severity])} aria-hidden />
      <span className="w-16 shrink-0 font-mono text-muted-foreground">{formatTimestamp(event.ts)}</span>
      <span className="font-mono font-medium">{event.kind}</span>
      {event.node && <span className="text-muted-foreground">node={event.node}</span>}
      {event.status && (
        <Badge variant="outline" className="font-mono text-[10px]">
          {event.status}
        </Badge>
      )}
      {event.error && typeof event.error.message === "string" && (
        <span className="truncate text-destructive">{event.error.message}</span>
      )}
    </div>
  );
}

function AttemptRow({
  block,
  onSelectStep,
  stepId,
}: {
  block: AttemptBlock;
  onSelectStep: (stepId: string) => void;
  stepId: string;
}) {
  const closedKind = block.closed?.kind;
  const failed = closedKind === "tool_failed" || closedKind === "tool_timeout";
  const verificationFailed = block.verifications.some((v) => v.kind === "verification_failed");

  return (
    <button
      type="button"
      onClick={() => onSelectStep(stepId)}
      className="flex w-full items-center gap-3 rounded py-1 pl-1 text-left text-xs hover:bg-accent/50"
    >
      <span
        className={cn(
          "h-1.5 w-1.5 shrink-0 rounded-full",
          failed ? "bg-destructive" : verificationFailed ? "bg-warning" : "bg-success"
        )}
        aria-hidden
      />
      <span className="w-16 shrink-0 font-mono text-muted-foreground">
        {block.started ? formatTimestamp(block.started.ts) : ""}
      </span>
      <span className="font-mono font-medium">attempt {block.attempt}</span>
      <span className={cn("font-mono", failed && "text-destructive")}>
        {block.closed?.status ?? "in progress"}
      </span>
      {block.closed?.duration_ms != null && (
        <span className="font-mono text-muted-foreground">{formatDuration(block.closed.duration_ms)}</span>
      )}
      {block.verifications.length > 0 && (
        <Badge variant={verificationFailed ? "warning" : "outline"} className="text-[10px]">
          verify: {block.verifications.at(-1)!.status}
        </Badge>
      )}
    </button>
  );
}

function RetryMarker({ event }: { event: TraceEventResource }) {
  const delayMs = (event.payload as Record<string, unknown> | undefined)?.delay_ms;
  return (
    <div className="ml-1 flex items-center gap-2 border-l-2 border-dashed border-warning/40 py-1 pl-4 text-xs text-warning">
      <ArrowRight className="h-3 w-3" aria-hidden />
      retry scheduled{typeof delayMs === "number" ? ` — waiting ${delayMs}ms` : ""}
      {event.error && typeof event.error.class === "string" && (
        <span className="text-muted-foreground">({event.error.class})</span>
      )}
    </div>
  );
}

function StepEventMarker({ event }: { event: TraceEventResource }) {
  return (
    <div className="ml-1 flex items-center gap-2 border-l-2 border-border py-1 pl-4 text-xs text-muted-foreground">
      <CircleDot className="h-3 w-3" aria-hidden />
      {event.kind}
      {event.status && <span className="font-mono">({event.status})</span>}
    </div>
  );
}

function StepGroupCard({
  stepId,
  tool,
  blocks,
  onSelectStep,
  selected,
}: {
  stepId: string;
  tool: string | null;
  blocks: (AttemptBlock | EventBlock)[];
  onSelectStep: (stepId: string) => void;
  selected: boolean;
}) {
  return (
    <div className={cn("rounded-md border border-border/60 p-2", selected && "border-primary/50 bg-accent/30")}>
      <div className="mb-1 flex items-center gap-2">
        <button
          type="button"
          onClick={() => onSelectStep(stepId)}
          className="font-mono text-xs font-semibold hover:text-primary"
        >
          {stepId}
        </button>
        {tool && (
          <Badge variant="outline" className="font-mono text-[10px]">
            {tool}
          </Badge>
        )}
      </div>
      <div className="flex flex-col">
        {blocks.map((block) =>
          block.kind === "attempt" ? (
            <AttemptRow key={`attempt-${block.attempt}`} block={block} onSelectStep={onSelectStep} stepId={stepId} />
          ) : block.event.kind === "retry_scheduled" ? (
            <RetryMarker key={block.event.seq} event={block.event} />
          ) : (
            <StepEventMarker key={block.event.seq} event={block.event} />
          )
        )}
      </div>
    </div>
  );
}

function ApprovalGapCard({ entry }: { entry: Extract<TimelineEntry, { kind: "approval-gap" }> }) {
  const toolFromPayload = entry.requested.tool;
  return (
    <div className="rounded-md border border-warning/40 bg-warning/5 p-3">
      <div className="flex items-center gap-2 text-warning">
        <Pause className="h-3.5 w-3.5" aria-hidden />
        <span className="text-xs font-semibold uppercase tracking-wide">Paused for approval</span>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        {toolFromPayload && (
          <>
            <span className="font-mono">{toolFromPayload}</span>{" "}
          </>
        )}
        {entry.stepId && <span className="font-mono">({entry.stepId})</span>} requested at{" "}
        {formatTimestamp(entry.requested.ts)}.
      </p>
      <p className="mt-1 text-xs">
        {entry.resolved ? (
          <>
            Resolved <span className="font-mono">{entry.resolved.kind.replace("approval_", "")}</span> after{" "}
            <span className="font-mono">{formatDuration(entry.waitMs)}</span> at{" "}
            {formatTimestamp(entry.resolved.ts)}.
          </>
        ) : (
          <span className="font-medium text-warning">Still waiting on a human decision.</span>
        )}
      </p>
    </div>
  );
}

export function ExecutionTimeline({
  events,
  selectedStepId,
  onSelectStep,
}: {
  events: TraceEventResource[];
  selectedStepId: string | null;
  onSelectStep: (stepId: string) => void;
}) {
  const timeline = buildTimeline(events);

  if (timeline.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Execution timeline</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No trace events recorded for this run yet.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Execution timeline</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {timeline.map((entry) => {
          if (entry.kind === "marker") {
            return <MarkerRow key={`m-${entry.seq}`} event={entry.event} />;
          }
          if (entry.kind === "approval-gap") {
            return <ApprovalGapCard key={`a-${entry.seq}`} entry={entry} />;
          }
          return (
            <StepGroupCard
              key={`s-${entry.stepId}`}
              stepId={entry.stepId}
              tool={entry.tool}
              blocks={entry.blocks}
              onSelectStep={onSelectStep}
              selected={entry.stepId === selectedStepId}
            />
          );
        })}
      </CardContent>
    </Card>
  );
}
