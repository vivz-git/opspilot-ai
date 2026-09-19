import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { decideApproval, getApproval } from "@/lib/api/client";
import {
  makeApprovedApproval,
  makePendingCustomerUpdateApproval,
  makePendingEmailApproval,
} from "@/test/fixtures/approvals";
import { renderWithQueryClient } from "@/test/test-utils";

import { ApprovalDetailView } from "./approval-detail-view";

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, getApproval: vi.fn(), decideApproval: vi.fn() };
});

const mockGetApproval = vi.mocked(getApproval);
const mockDecideApproval = vi.mocked(decideApproval);

describe("ApprovalDetailView", () => {
  beforeEach(() => {
    mockGetApproval.mockReset();
    mockDecideApproval.mockReset();
  });

  // --- loading / not-found / error -------------------------------------------------------------

  it("shows a loading skeleton before the approval arrives", () => {
    mockGetApproval.mockImplementation(() => new Promise(() => {}));
    renderWithQueryClient(<ApprovalDetailView approvalId="missing" />);

    expect(screen.queryByText("What will happen")).not.toBeInTheDocument();
  });

  it("shows a distinct not-found message for a 404", async () => {
    const { ApiError } = await import("@/lib/api/client");
    mockGetApproval.mockRejectedValue(new ApiError(404, "Not Found", { code: "not_found" }));
    renderWithQueryClient(<ApprovalDetailView approvalId="missing-approval" />);

    expect(await screen.findByText("Approval not found")).toBeInTheDocument();
    expect(screen.getByText(/No approval exists with id missing-approval/)).toBeInTheDocument();
  });

  it("shows a generic error state for a non-404 failure", async () => {
    mockGetApproval.mockRejectedValue(new Error("Failed to fetch"));
    renderWithQueryClient(<ApprovalDetailView approvalId="some-approval" />);

    expect(await screen.findByText("Could not load this approval")).toBeInTheDocument();
  });

  // --- header / evidence -------------------------------------------------------------

  it("renders title, summary, tool, risk, status and the server's exact args_hash", async () => {
    const approval = makePendingEmailApproval();
    mockGetApproval.mockResolvedValue(approval);
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    expect(await screen.findByText(approval.title)).toBeInTheDocument();
    expect(screen.getByText(approval.summary)).toBeInTheDocument();
    expect(screen.getByText(approval.tool)).toBeInTheDocument();
    expect(screen.getByText(approval.args_hash)).toBeInTheDocument();
    expect(screen.getByText("pending")).toBeInTheDocument();
  });

  // --- payload preview: email -------------------------------------------------------------

  it("renders the full email body and resolved arguments for a send_email_mock approval", async () => {
    const approval = makePendingEmailApproval();
    mockGetApproval.mockResolvedValue(approval);
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await screen.findByText("What will happen");
    expect(screen.getByText(/Quick question about your fintech stack/)).toBeInTheDocument();
    expect(screen.getByText(/I noticed Acme Corp has been expanding/)).toBeInTheDocument();
    expect(screen.getByText("jane@acmecorp.com")).toBeInTheDocument();
    expect(screen.getByText(/"draft_id": "draft-771"/)).toBeInTheDocument();
  });

  // --- payload preview: customer update -------------------------------------------------------------

  it("renders the field diff and version-mismatch warning for an update_customer approval", async () => {
    const approval = makePendingCustomerUpdateApproval();
    mockGetApproval.mockResolvedValue(approval);
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await screen.findByText("What will happen");
    expect(screen.getByText(/Version mismatch/)).toBeInTheDocument();
    expect(screen.getByText(/"j.doe@acmecorp.com"/)).toBeInTheDocument();
    expect(screen.getAllByText(/"jane@acmecorp.com"/).length).toBeGreaterThan(0);
  });

  // --- approve flow -------------------------------------------------------------

  it("approves using the exact args_hash already on screen, never recomputing it", async () => {
    const approval = makePendingEmailApproval();
    mockGetApproval.mockResolvedValue(approval);
    mockDecideApproval.mockResolvedValue(makeApprovedApproval());
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(mockDecideApproval).toHaveBeenCalledWith(approval.approval_id, {
        decision: "approve",
        args_hash: approval.args_hash,
      })
    );
  });

  it("disables the decision form once the server reports the approval is no longer pending", async () => {
    const approval = makePendingEmailApproval();
    mockGetApproval.mockResolvedValueOnce(approval).mockResolvedValue(makeApprovedApproval());
    mockDecideApproval.mockResolvedValue(makeApprovedApproval());
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Approve" }));

    expect(await screen.findByText(/can no longer be decided from this screen/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  // --- reject flow: required reason -------------------------------------------------------------

  it("requires a non-blank rejection reason before submitting a reject decision", async () => {
    const approval = makePendingEmailApproval();
    mockGetApproval.mockResolvedValue(approval);
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Reject" }));
    await user.click(screen.getByRole("button", { name: "Confirm reject" }));

    expect(screen.getByText("A reason is required to reject.")).toBeInTheDocument();
    expect(mockDecideApproval).not.toHaveBeenCalled();
  });

  it("rejects with the typed reason once provided", async () => {
    const approval = makePendingEmailApproval();
    mockGetApproval.mockResolvedValue(approval);
    mockDecideApproval.mockResolvedValue({ ...approval, status: "rejected" });
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Reject" }));
    await user.type(screen.getByLabelText(/Rejection reason/), "Wrong recipient");
    await user.click(screen.getByRole("button", { name: "Confirm reject" }));

    await waitFor(() =>
      expect(mockDecideApproval).toHaveBeenCalledWith(approval.approval_id, {
        decision: "reject",
        args_hash: approval.args_hash,
        reason: "Wrong recipient",
      })
    );
  });

  // --- conflict codes -------------------------------------------------------------

  const CONFLICT_CASES: Array<{ code: string; text: RegExp }> = [
    { code: "approval_not_pending", text: /already decided/ },
    { code: "approval_expired", text: /expired before a decision/ },
    { code: "approval_superseded", text: /screen is stale/ },
    { code: "run_not_resumable", text: /no longer in a state that can resume/ },
  ];

  for (const { code, text } of CONFLICT_CASES) {
    it(`surfaces a distinct, calm message for the ${code} conflict without navigating away`, async () => {
      const approval = makePendingEmailApproval();
      mockGetApproval.mockResolvedValue(approval);
      const { ApiError } = await import("@/lib/api/client");
      mockDecideApproval.mockRejectedValue(new ApiError(409, "Conflict", { code }));
      const user = userEvent.setup();
      renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

      await user.click(await screen.findByRole("button", { name: "Approve" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(text);
      // Still on the same approval — no silent switch to a different one.
      for (const call of mockGetApproval.mock.calls) {
        expect(call[0]).toBe(approval.approval_id);
      }
    });
  }

  // --- keyboard accessibility -------------------------------------------------------------

  it("exposes the rejection reason field via an associated label for keyboard/screen-reader users", async () => {
    const approval = makePendingEmailApproval();
    mockGetApproval.mockResolvedValue(approval);
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Reject" }));
    const field = screen.getByLabelText(/Rejection reason/);
    expect(field).toBeInTheDocument();
    field.focus();
    expect(field).toHaveFocus();
  });

  it("re-fetches the same approval from the API on every mount — no client-only state to reconstruct from", async () => {
    const approval = makePendingEmailApproval();
    mockGetApproval.mockResolvedValue(approval);
    const { unmount } = renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);
    await screen.findByText("What will happen");
    expect(mockGetApproval).toHaveBeenCalledTimes(1);

    unmount();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);
    await screen.findByText("What will happen");

    expect(mockGetApproval).toHaveBeenCalledTimes(2);
  });
});
