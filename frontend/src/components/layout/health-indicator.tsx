"use client";

import { Badge } from "@/components/ui/badge";
import { useHealthz } from "@/lib/api/queries";

export function HealthIndicator() {
  const { data, isError, isPending } = useHealthz();

  if (isPending) {
    return (
      <Badge variant="outline" className="animate-pulse">
        checking API…
      </Badge>
    );
  }

  if (isError) {
    return <Badge variant="destructive">API unreachable</Badge>;
  }

  return <Badge variant="success">{data?.status ?? "ok"}</Badge>;
}
