import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { listApprovalQueue } from "@/lib/api/client";
import { APPROVAL_QUEUE_EMPTY, APPROVAL_QUEUE_FIXTURE } from "@/test/fixtures/approvals";
import { renderWithQueryClient } from "@/test/test-utils";

import ApprovalsPage from "./page";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, listApprovalQueue: vi.fn() };
});

const mockListApprovalQueue = vi.mocked(listApprovalQueue);

describe("ApprovalsPage", () => {
  beforeEach(() => {
    push.mockClear();
    mockListApprovalQueue.mockReset();
  });

  it("shows a loading skeleton before data arrives", () => {
    mockListApprovalQueue.mockImplementation(() => new Promise(() => {}));
    renderWithQueryClient(<ApprovalsPage />);

    expect(screen.getByText("Approvals")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders populated approvals with title, tool, risk and status", async () => {
    mockListApprovalQueue.mockResolvedValue(APPROVAL_QUEUE_FIXTURE);
    renderWithQueryClient(<ApprovalsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("link");
    expect(rows).toHaveLength(2);

    const firstRow = rows[0]!;
    expect(within(firstRow).getByText(/Send outreach email/)).toBeInTheDocument();
    expect(within(firstRow).getByText("send_email_mock")).toBeInTheDocument();
    expect(within(firstRow).getByText("high")).toBeInTheDocument();
  });

  it("shows an empty state distinct from an error when nothing is pending", async () => {
    mockListApprovalQueue.mockResolvedValue(APPROVAL_QUEUE_EMPTY);
    renderWithQueryClient(<ApprovalsPage />);

    expect(await screen.findByText(/Nothing pending/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows an error card when the queue request fails", async () => {
    mockListApprovalQueue.mockRejectedValue(new Error("Failed to fetch"));
    renderWithQueryClient(<ApprovalsPage />);

    expect(await screen.findByText("Could not load the approval queue")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("navigates to the approval detail route when a row is selected", async () => {
    mockListApprovalQueue.mockResolvedValue(APPROVAL_QUEUE_FIXTURE);
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalsPage />);

    const table = await screen.findByRole("table");
    const firstRow = within(table).getAllByRole("link")[0]!;
    await user.click(firstRow);

    expect(push).toHaveBeenCalledWith(`/approvals/${APPROVAL_QUEUE_FIXTURE[0]!.approval_id}`);
  });

  it("navigates on Enter for keyboard users", async () => {
    mockListApprovalQueue.mockResolvedValue(APPROVAL_QUEUE_FIXTURE);
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalsPage />);

    const table = await screen.findByRole("table");
    const firstRow = within(table).getAllByRole("link")[0]!;
    firstRow.focus();
    await user.keyboard("{Enter}");

    expect(push).toHaveBeenCalledWith(`/approvals/${APPROVAL_QUEUE_FIXTURE[0]!.approval_id}`);
  });
});
