import { EvaluationRunDetailView } from "@/components/evaluations/evaluation-run-detail-view";

export default async function EvaluationRunDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  return <EvaluationRunDetailView evaluationRunId={id} />;
}
