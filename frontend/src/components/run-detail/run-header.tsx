import { StatusBadge } from "@/components/status-badge";
import { Card, CardContent } from "@/components/ui/card";
import { isLiveStatus } from "@/components/status-badge";
import type { RunResource } from "@/lib/api/client";
import { deriveRunDurationMs, formatDuration, formatTimestamp } from "@/lib/format";

function Stat({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className={mono ? "font-mono text-sm" : "text-sm font-medium"}>{value}</span>
    </div>
  );
}

export function RunHeader({ run }: { run: RunResource }) {
  const { timestamps, counters } = run;
  const durationMs = deriveRunDurationMs(timestamps.started_at ?? null, timestamps.finished_at ?? null);

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 pt-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 flex-col gap-1">
            <p className="max-w-2xl text-sm font-medium leading-relaxed">{run.user_request}</p>
            <span className="font-mono text-xs text-muted-foreground">{run.run_id}</span>
          </div>
          <div className="flex flex-col items-end gap-1">
            <StatusBadge status={run.status} />
            {run.status_reason && (
              <span className="font-mono text-xs text-muted-foreground">{run.status_reason}</span>
            )}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4 border-t border-border/60 pt-4 sm:grid-cols-3 lg:grid-cols-6">
          <Stat
            label="Duration"
            value={
              durationMs == null && isLiveStatus(run.status) ? (
                <span className="text-info">running…</span>
              ) : (
                formatDuration(durationMs)
              )
            }
            mono
          />
          <Stat label="Steps" value={counters.step_count} mono />
          <Stat label="Retries" value={counters.retry_total} mono />
          <Stat label="Created" value={formatTimestamp(timestamps.created_at)} mono />
          <Stat
            label="Started"
            value={timestamps.started_at ? formatTimestamp(timestamps.started_at) : "—"}
            mono
          />
          <Stat
            label="Finished"
            value={timestamps.finished_at ? formatTimestamp(timestamps.finished_at) : "—"}
            mono
          />
        </div>
      </CardContent>
    </Card>
  );
}
