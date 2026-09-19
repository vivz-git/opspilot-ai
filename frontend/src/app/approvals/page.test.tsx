import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { listApprovalQueue } from "@/lib/api/client";
import { APPROVAL_QUEUE_FIXTURE } from "@/test/fixtures/approvals";
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

    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows an empty state with nothing pending", async () => {
    mockListApprovalQueue.mockResolvedValue([]);
    renderWithQueryClient(<ApprovalsPage />);

    expect(await screen.findByText(/Nothing pending/)).toBeInTheDocument();
  });

  it("shows an error state on failure", async () => {
    mockListApprovalQueue.mockRejectedValue(new Error("Failed to fetch"));
    renderWithQueryClient(<ApprovalsPage />);

    expect(await screen.findByText("Could not load the approval queue")).toBeInTheDocument();
  });

  it("renders queued approvals with tool and risk", async () => {
    mockListApprovalQueue.mockResolvedValue(APPROVAL_QUEUE_FIXTURE);
    renderWithQueryClient(<ApprovalsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("link");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("send_email_mock")).toBeInTheDocument();
  });

  it("navigates to the approval detail page on row activation", async () => {
    mockListApprovalQueue.mockResolvedValue(APPROVAL_QUEUE_FIXTURE);
    const user = userEvent.setup();
    renderWithQueryClient(<ApprovalsPage />);

    const table = await screen.findByRole("table");
    await user.click(within(table).getAllByRole("link")[0]!);

    expect(push).toHaveBeenCalledWith(
      `/approvals/${APPROVAL_QUEUE_FIXTURE[0]!.approval_id}`
    );
  });

  it("is keyboard-activatable via Enter", async () => {
    mockListApprovalQueue.mockResolvedValue(APPROVAL_QUEUE_FIXTURE);
    renderWithQueryClient(<ApprovalsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("link");
    rows[0]!.focus();
    await userEvent.keyboard("{Enter}");

    expect(push).toHaveBeenCalledWith(
      `/approvals/${APPROVAL_QUEUE_FIXTURE[0]!.approval_id}`
    );
  });
});
