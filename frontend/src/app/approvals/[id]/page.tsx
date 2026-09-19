import { ApprovalDetailView } from "@/components/approvals/approval-detail-view";

export default async function ApprovalDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  return <ApprovalDetailView approvalId={id} />;
}
