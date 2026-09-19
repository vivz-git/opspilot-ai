import { Badge, type BadgeProps } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type StatusTone = NonNullable<BadgeProps["variant"]> | "info";

/**
 * Status vocabulary comes from the backend's RunStatus / ApprovalStatus
 * enums (architecture.md §5.4, §9) — the frontend renders it, it doesn't
 * invent its own. `live` states get a pulsing dot (real-time-monitoring
 * convention: an in-progress state should visibly differ from a settled
 * one, not just by color).
 */
const TONE_BY_STATUS: Record<string, { tone: StatusTone; live?: boolean }> = {
  completed: { tone: "success" },
  approved: { tone: "success" },
  running: { tone: "info", live: true },
  queued: { tone: "outline" },
  created: { tone: "outline" },
  pending: { tone: "warning", live: true },
  awaiting_approval: { tone: "warning", live: true },
  failed: { tone: "destructive" },
  rejected: { tone: "destructive" },
  cancelled: { tone: "secondary" },
  expired: { tone: "secondary" },
  superseded: { tone: "secondary" },
};

/** Whether a status is still in flight (used to distinguish "no duration yet" from "settled"). */
export function isLiveStatus(status: string): boolean {
  return TONE_BY_STATUS[status]?.live === true;
}

const DOT_CLASS_BY_TONE: Record<StatusTone, string> = {
  success: "bg-success",
  info: "bg-info",
  warning: "bg-warning",
  destructive: "bg-destructive",
  secondary: "bg-muted-foreground",
  outline: "bg-muted-foreground",
  default: "bg-foreground",
};

export function StatusBadge({ status }: { status: string }) {
  const { tone, live } = TONE_BY_STATUS[status] ?? { tone: "outline" as const };
  const badgeVariant = tone === "info" ? "outline" : tone;

  return (
    <Badge
      variant={badgeVariant}
      className={cn(
        "gap-1.5 font-mono",
        tone === "info" && "border-info/40 text-info"
      )}
    >
      <span
        className={cn(
          "h-1.5 w-1.5 shrink-0 rounded-full",
          DOT_CLASS_BY_TONE[tone],
          live && "animate-live-pulse"
        )}
        aria-hidden
      />
      {status}
    </Badge>
  );
}
