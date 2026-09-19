import { Badge, type BadgeProps } from "@/components/ui/badge";

const VARIANT_BY_STATUS: Record<string, NonNullable<BadgeProps["variant"]>> = {
  completed: "success",
  approved: "success",
  running: "default",
  queued: "outline",
  created: "outline",
  pending: "warning",
  awaiting_approval: "warning",
  failed: "destructive",
  rejected: "destructive",
  cancelled: "secondary",
  expired: "secondary",
  superseded: "secondary",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <Badge variant={VARIANT_BY_STATUS[status] ?? "outline"} className="font-mono">
      {status}
    </Badge>
  );
}
