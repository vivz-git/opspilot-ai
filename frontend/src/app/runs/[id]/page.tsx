import { Activity } from "lucide-react";

import { ComingSoon } from "@/components/coming-soon";

export default async function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  return (
    <ComingSoon
      icon={Activity}
      title={`Run ${id}`}
      description="Plan-vs-actual timeline, per-step tool IO, retries, verification badges, inline approval card."
      note="Run detail (FE-003) isn't built yet. This route exists so the Runs list has somewhere real to send you, rather than a dead link."
    />
  );
}
