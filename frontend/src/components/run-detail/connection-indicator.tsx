import { cn } from "@/lib/utils";
import type { SseConnectionStatus } from "@/lib/api/use-run-events";

const LABEL: Record<SseConnectionStatus, string> = {
  idle: "static",
  connecting: "connecting",
  open: "live",
  reconnecting: "reconnecting",
  disconnected: "disconnected",
};

const DOT_CLASS: Record<SseConnectionStatus, string> = {
  idle: "bg-muted-foreground",
  connecting: "bg-muted-foreground",
  open: "bg-success",
  reconnecting: "bg-warning",
  disconnected: "bg-muted-foreground",
};

/**
 * A calm, secondary indicator of the SSE connection (FE-007) — deliberately
 * small and muted text rather than a badge, so it never competes with the
 * run's own `StatusBadge` for attention. Only `open` pulses; a
 * `connecting`/`reconnecting` state should read as quiet, not alarming.
 */
export function ConnectionIndicator({ status }: { status: SseConnectionStatus }) {
  if (status === "idle") return null;

  return (
    <span className="flex items-center gap-1.5 text-xs text-muted-foreground" title={`Live updates: ${LABEL[status]}`}>
      <span
        className={cn(
          "h-1.5 w-1.5 shrink-0 rounded-full",
          DOT_CLASS[status],
          status === "open" && "animate-live-pulse"
        )}
        aria-hidden
      />
      {LABEL[status]}
    </span>
  );
}
