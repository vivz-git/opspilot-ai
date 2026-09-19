import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { decideApproval, getApproval } from "@/lib/api/client";
import { makeApproval } from "@/test/fixtures/approvals";
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

  // --- loading / not-found / error ------------------------------------------

  it("shows a loading state before the approval arrives", () => {
    mockGetApproval.mockImplementation(() => new Promise(() => {}));
    renderWithQueryClient(<ApprovalDetailView approvalId="a1" />);

    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("shows a distinct not-found message for a 404", async () => {
    const { ApiError } = await import("@/lib/api/client");
    mockGetApproval.mockRejectedValue(new ApiError(404, "Not Found", { code: "not_found" }));
    renderWithQueryClient(<ApprovalDetailView approvalId="missing" />);

    expect(await screen.findByText("Approval not found")).toBeInTheDocument();
    expect(screen.getByText(/No approval exists with id missing/)).toBeInTheDocument();
  });

  it("shows a generic error state for a non-404 failure", async () => {
    mockGetApproval.mockRejectedValue(new Error("Failed to fetch"));
    renderWithQueryClient(<ApprovalDetailView approvalId="a1" />);

    expect(await screen.findByText("Could not load this approval")).toBeInTheDocument();
  });

  // --- rendering -------------------------------------------------------------

  it("renders the approval's title, summary, risk, payload and run link", async () => {
    const approval = makeApproval();
    mockGetApproval.mockResolvedValue(approval);
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    expect(await screen.findByText(approval.title)).toBeInTheDocument();
    expect(screen.getByText(approval.summary)).toBeInTheDocument();
    expect(screen.getByText("high")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: approval.run_id })).toHaveAttribute(
      "href",
      `/runs/${approval.run_id}`
    );
    expect(screen.getByText(/"to"/)).toBeInTheDocument();
  });

  it("shows no decision actions once an approval is already settled", async () => {
    const approval = makeApproval({
      status: "approved",
      decided_at: "2026-09-19T09:00:00Z",
      decided_by: "operator@example.com",
    });
    mockGetApproval.mockResolvedValue(approval);
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await screen.findByText(approval.title);
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    expect(screen.getByText("operator@example.com")).toBeInTheDocument();
  });

  // --- approve ---------------------------------------------------------------

  it("approves with the displayed args_hash", async () => {
    const approval = makeApproval();
    mockGetApproval.mockResolvedValue(approval);
    mockDecideApproval.mockResolvedValue({ ...approval, status: "approved" });
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Approve" }));

    expect(mockDecideApproval).toHaveBeenCalledWith(approval.approval_id, {
      decision: "approve",
      args_hash: approval.args_hash,
    });
  });

  // --- reject ------------------------------------------------------------

  it("requires a reason before rejecting can be confirmed", async () => {
    const approval = makeApproval();
    mockGetApproval.mockResolvedValue(approval);
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Reject" }));
    const confirm = screen.getByRole("button", { name: "Confirm reject" });
    expect(confirm).toBeDisabled();

    await user.type(screen.getByLabelText(/Reason for rejecting/), "Wrong recipient");
    expect(confirm).toBeEnabled();
  });

  it("rejects with the entered reason", async () => {
    const approval = makeApproval();
    mockGetApproval.mockResolvedValue(approval);
    mockDecideApproval.mockResolvedValue({ ...approval, status: "rejected" });
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Reject" }));
    await user.type(screen.getByLabelText(/Reason for rejecting/), "Wrong recipient");
    await user.click(screen.getByRole("button", { name: "Confirm reject" }));

    expect(mockDecideApproval).toHaveBeenCalledWith(approval.approval_id, {
      decision: "reject",
      args_hash: approval.args_hash,
      reason: "Wrong recipient",
    });
  });

  it("cancels out of the reject flow without submitting anything", async () => {
    const approval = makeApproval();
    mockGetApproval.mockResolvedValue(approval);
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Reject" }));
    await user.type(screen.getByLabelText(/Reason for rejecting/), "typing something");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
    expect(mockDecideApproval).not.toHaveBeenCalled();
  });

  // --- the conflict family (§13.5 API-004), each a distinct code -----------

  it.each([
    ["approval_not_pending", /no longer pending/],
    ["approval_expired", /expired before a decision/],
    ["approval_superseded", /superseded/],
    ["run_not_resumable", /no longer resumable/],
  ] as const)("shows a specific message for %s", async (code, expected) => {
    const { ApiError } = await import("@/lib/api/client");
    const approval = makeApproval();
    mockGetApproval.mockResolvedValue(approval);
    mockDecideApproval.mockRejectedValue(
      new ApiError(409, "Conflict", { code, detail: "server detail text" })
    );
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Approve" }));

    const banner = await screen.findByText(expected);
    expect(within(banner.closest("div")!).queryByText("server detail text")).not.toBeInTheDocument();
  });

  it("shows the run's own error message when the code is not one of the known conflict codes", async () => {
    const { ApiError } = await import("@/lib/api/client");
    const approval = makeApproval();
    mockGetApproval.mockResolvedValue(approval);
    mockDecideApproval.mockRejectedValue(
      new ApiError(500, "Internal Server Error", {
        code: "internal_error",
        detail: "an unexpected internal error occurred",
      })
    );
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalDetailView approvalId={approval.approval_id} />);

    await user.click(await screen.findByRole("button", { name: "Approve" }));

    expect(await screen.findByText("an unexpected internal error occurred")).toBeInTheDocument();
  });
});
