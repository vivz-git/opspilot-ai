"use client";

import { cn } from "@/lib/utils";
import { useHealthz } from "@/lib/api/queries";

export function HealthIndicator() {
  const { data, isError, isPending } = useHealthz();

  const label = isPending ? "connecting" : isError ? "api unreachable" : (data?.status ?? "ok");
  const dotClass = isPending
    ? "bg-muted-foreground"
    : isError
      ? "bg-destructive"
      : "bg-success animate-live-pulse";

  return (
    <div className="flex items-center gap-2 rounded-md border border-border/60 px-2.5 py-1">
      <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", dotClass)} aria-hidden />
      <span className="font-mono text-xs text-muted-foreground">{label}</span>
    </div>
  );
}
