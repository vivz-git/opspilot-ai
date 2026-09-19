"use client";

import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { RunResource, RunStepSummary } from "@/lib/api/client";
import { formatDuration } from "@/lib/format";
import { buildPlanVsActual, parsePlannedSteps, type PlanVsActualRow } from "@/lib/plan";
import { cn } from "@/lib/utils";

function ActualCells({ actual }: { actual: RunStepSummary | null }) {
  if (!actual) {
    return (
      <>
        <TableCell colSpan={3} className="text-xs italic text-muted-foreground">
          Planned — not yet run
        </TableCell>
      </>
    );
  }
  return (
    <>
      <TableCell className="text-right font-mono text-xs">
        {actual.attempts}
        {actual.retry_count > 0 && (
          <span className="ml-1 text-muted-foreground">({actual.retry_count} retry)</span>
        )}
      </TableCell>
      <TableCell className="text-right font-mono text-xs">{formatDuration(actual.duration_ms)}</TableCell>
      <TableCell>
        {actual.verification_status ? (
          <StatusBadge status={actual.verification_status} />
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        )}
      </TableCell>
    </>
  );
}

function PlanRow({
  row,
  selectedStepId,
  onSelectStep,
}: {
  row: PlanVsActualRow;
  selectedStepId: string | null;
  onSelectStep: (stepId: string) => void;
}) {
  // A step that never ran is still selectable — the inspector shows that
  // plainly rather than the row being a dead end. The one exception is a
  // fan-out template that *has* expanded: the template itself isn't a real
  // execution unit once its children exist, so selection goes to them.
  const selectableStepId = row.isFanoutTemplate && row.fanoutChildren.length > 0 ? null : row.step.step_id;
  const selected = selectableStepId != null && selectableStepId === selectedStepId;

  return (
    <>
      <TableRow
        role={selectableStepId ? "button" : undefined}
        tabIndex={selectableStepId ? 0 : undefined}
        aria-pressed={selectableStepId ? selected : undefined}
        onClick={selectableStepId ? () => onSelectStep(selectableStepId) : undefined}
        onKeyDown={
          selectableStepId
            ? (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelectStep(selectableStepId);
                }
              }
            : undefined
        }
        className={cn(
          selectableStepId && "cursor-pointer",
          selected && "bg-accent",
          !row.ran && "opacity-50"
        )}
      >
        <TableCell className="font-mono text-xs text-muted-foreground">{row.step.step_id}</TableCell>
        <TableCell>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="font-mono">
              {row.step.tool}
            </Badge>
            {row.isFanoutTemplate && (
              <Badge variant="secondary" className="text-[10px]">
                fan-out
              </Badge>
            )}
            {row.step.optional && (
              <Badge variant="secondary" className="text-[10px]">
                optional
              </Badge>
            )}
          </div>
          {row.step.rationale && (
            <p className="mt-1 max-w-md truncate text-xs text-muted-foreground">{row.step.rationale}</p>
          )}
        </TableCell>
        {row.isFanoutTemplate && row.fanoutChildren.length > 0 ? (
          <TableCell colSpan={3} className="text-xs text-muted-foreground">
            {row.fanoutChildren.length} expanded step{row.fanoutChildren.length === 1 ? "" : "s"} — select
            below
          </TableCell>
        ) : (
          <ActualCells actual={row.actual} />
        )}
      </TableRow>
      {row.fanoutChildren.map((child) => {
        const childSelected = child.step_id === selectedStepId;
        return (
          <TableRow
            key={child.step_id}
            role="button"
            tabIndex={0}
            aria-pressed={childSelected}
            onClick={() => onSelectStep(child.step_id)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelectStep(child.step_id);
              }
            }}
            className={cn("cursor-pointer", childSelected && "bg-accent")}
          >
            <TableCell className="pl-6 font-mono text-xs text-muted-foreground">{child.step_id}</TableCell>
            <TableCell className="text-xs text-muted-foreground">expanded child</TableCell>
            <ActualCells actual={child} />
          </TableRow>
        );
      })}
    </>
  );
}

export function PlanVsActual({
  run,
  selectedStepId,
  onSelectStep,
}: {
  run: RunResource;
  selectedStepId: string | null;
  onSelectStep: (stepId: string) => void;
}) {
  const planned = parsePlannedSteps(run.plan);
  const { rows, unplanned } = buildPlanVsActual(planned, run.steps ?? []);

  if (planned.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Plan vs. actual</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No plan has been recorded for this run yet.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden p-0">
      <CardHeader className="border-b border-border/60 py-4">
        <CardTitle className="text-base">Plan vs. actual</CardTitle>
      </CardHeader>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Step</TableHead>
            <TableHead>Tool</TableHead>
            <TableHead className="text-right">Attempts</TableHead>
            <TableHead className="text-right">Duration</TableHead>
            <TableHead>Verification</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <PlanRow key={row.step.step_id} row={row} selectedStepId={selectedStepId} onSelectStep={onSelectStep} />
          ))}
        </TableBody>
      </Table>
      {unplanned.length > 0 && (
        <div className="border-t border-border/60 p-3">
          <p className="mb-2 text-xs text-muted-foreground">
            Executed under a prior plan revision (not in the current plan snapshot):
          </p>
          <div className="flex flex-wrap gap-1.5">
            {unplanned.map((s) => (
              <button key={s.step_id} type="button" onClick={() => onSelectStep(s.step_id)}>
                <Badge variant="outline" className="cursor-pointer font-mono">
                  {s.step_id}
                </Badge>
              </button>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}
