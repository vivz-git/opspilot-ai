import { RunDetailView } from "@/components/run-detail/run-detail-view";

export default async function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  return <RunDetailView runId={id} />;
}
