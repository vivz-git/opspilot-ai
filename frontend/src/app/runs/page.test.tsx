import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { listRuns } from "@/lib/api/client";
import {
  RUNS_FIXTURE_EMPTY,
  RUNS_FIXTURE_PAGE_1,
  RUNS_FIXTURE_PAGE_2,
} from "@/test/fixtures/runs";
import { renderWithQueryClient } from "@/test/test-utils";

import RunsPage from "./page";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, listRuns: vi.fn() };
});

const mockListRuns = vi.mocked(listRuns);

describe("RunsPage", () => {
  beforeEach(() => {
    push.mockClear();
    mockListRuns.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // --- loading -------------------------------------------------------------

  it("shows a loading skeleton before data arrives", () => {
    mockListRuns.mockImplementation(() => new Promise(() => {}));
    renderWithQueryClient(<RunsPage />);

    expect(screen.getByText("Runs")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  // --- populated rendering ---------------------------------------------------

  it("renders populated runs with request, duration, steps, retries and status", async () => {
    mockListRuns.mockResolvedValue(RUNS_FIXTURE_PAGE_1);
    renderWithQueryClient(<RunsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("link"); // TableRow: role="link"
    expect(rows).toHaveLength(2);

    const firstRow = rows[0]!;
    expect(within(firstRow).getByText(/Find the top 3 fintech leads/)).toBeInTheDocument();
    expect(within(firstRow).getByText("4")).toBeInTheDocument(); // step_count
    expect(within(firstRow).getByText("0")).toBeInTheDocument(); // retry_total
    // Still running: no settled duration, shows the live indicator instead.
    expect(within(firstRow).getByText("running…")).toBeInTheDocument();
  });

  it("renders a settled duration for a terminal run", async () => {
    mockListRuns.mockResolvedValue(RUNS_FIXTURE_PAGE_2);
    renderWithQueryClient(<RunsPage />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("5m 2s")).toBeInTheDocument(); // 302_000ms
  });

  // --- status variants -------------------------------------------------------

  it("renders the correct status badge text per run status", async () => {
    mockListRuns.mockResolvedValue(RUNS_FIXTURE_PAGE_1);
    renderWithQueryClient(<RunsPage />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("running")).toBeInTheDocument();
    expect(within(table).getByText("awaiting_approval")).toBeInTheDocument();
  });

  // --- empty state -------------------------------------------------------

  it("shows an empty state distinct from an error when there are no runs", async () => {
    mockListRuns.mockResolvedValue(RUNS_FIXTURE_EMPTY);
    renderWithQueryClient(<RunsPage />);

    expect(await screen.findByText(/No runs yet/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows a filters-aware empty message when filters are active", async () => {
    mockListRuns.mockResolvedValue(RUNS_FIXTURE_EMPTY);
    const user = userEvent.setup();
    renderWithQueryClient(<RunsPage />);

    await user.click(await screen.findByRole("button", { name: "failed" }));
    expect(await screen.findByText(/No runs match these filters/)).toBeInTheDocument();
  });

  // --- API error -------------------------------------------------------

  it("shows an error card when the API request fails", async () => {
    mockListRuns.mockRejectedValue(new Error("Failed to fetch"));
    renderWithQueryClient(<RunsPage />);

    expect(await screen.findByText("Could not load runs")).toBeInTheDocument();
    expect(screen.getByText(/Failed to fetch/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  // --- filters map to GET /runs query parameters -------------------------------------------------------

  it("maps a status filter toggle directly to the status query parameter", async () => {
    mockListRuns.mockResolvedValue(RUNS_FIXTURE_PAGE_1);
    const user = userEvent.setup();
    renderWithQueryClient(<RunsPage />);

    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "failed" }));

    await waitFor(() => {
      expect(mockListRuns).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: ["failed"] })
      );
    });
  });

  it("debounces the search box and sends it as the q parameter", async () => {
    vi.useFakeTimers();
    mockListRuns.mockResolvedValue(RUNS_FIXTURE_PAGE_1);
    renderWithQueryClient(<RunsPage />);

    fireEvent.change(screen.getByLabelText("Search run request text"), {
      target: { value: "fintech" },
    });
    // Not yet — still inside the debounce window.
    expect(mockListRuns).not.toHaveBeenCalledWith(expect.objectContaining({ q: "fintech" }));

    await act(() => vi.advanceTimersByTimeAsync(400));
    expect(mockListRuns).toHaveBeenCalledWith(expect.objectContaining({ q: "fintech" }));
  });

  it("resets pagination when a filter changes", async () => {
    mockListRuns.mockResolvedValueOnce(RUNS_FIXTURE_PAGE_1).mockResolvedValue(RUNS_FIXTURE_PAGE_2);
    const user = userEvent.setup();
    renderWithQueryClient(<RunsPage />);

    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() =>
      expect(mockListRuns).toHaveBeenLastCalledWith(
        expect.objectContaining({ cursor: RUNS_FIXTURE_PAGE_1.next_cursor })
      )
    );

    await user.click(screen.getByRole("button", { name: "failed" }));
    await waitFor(() => {
      const lastCall = mockListRuns.mock.calls.at(-1)?.[0];
      expect(lastCall).toMatchObject({ status: ["failed"] });
      expect(lastCall?.cursor).toBeUndefined();
    });
  });

  // --- keyset pagination -------------------------------------------------------

  it("does not enable Next when the server returns no next_cursor", async () => {
    mockListRuns.mockResolvedValue(RUNS_FIXTURE_PAGE_2); // next_cursor: null
    renderWithQueryClient(<RunsPage />);

    await screen.findByRole("table");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
  });

  it("pages forward using the server's next_cursor, never a client-computed offset", async () => {
    mockListRuns.mockResolvedValueOnce(RUNS_FIXTURE_PAGE_1).mockResolvedValue(RUNS_FIXTURE_PAGE_2);
    const user = userEvent.setup();
    renderWithQueryClient(<RunsPage />);

    await screen.findByRole("table");
    expect(mockListRuns).toHaveBeenLastCalledWith(
      expect.not.objectContaining({ cursor: expect.anything() })
    );

    await user.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() =>
      expect(mockListRuns).toHaveBeenLastCalledWith(
        expect.objectContaining({ cursor: RUNS_FIXTURE_PAGE_1.next_cursor })
      )
    );
    await screen.findByText(/Research companies for leads/);
  });

  it("pages backward by returning to the cursor already seen, without re-deriving one", async () => {
    mockListRuns.mockResolvedValueOnce(RUNS_FIXTURE_PAGE_1).mockResolvedValueOnce(RUNS_FIXTURE_PAGE_2);
    const user = userEvent.setup();
    renderWithQueryClient(<RunsPage />);

    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText(/Research companies for leads/);

    mockListRuns.mockResolvedValue(RUNS_FIXTURE_PAGE_1);
    await user.click(screen.getByRole("button", { name: "Previous" }));

    await waitFor(() =>
      expect(mockListRuns).toHaveBeenLastCalledWith(
        expect.not.objectContaining({ cursor: expect.anything() })
      )
    );
    await screen.findByText(/Find the top 3 fintech leads/);
  });

  // --- navigation -------------------------------------------------------

  it("navigates to the run-detail route when a row is selected", async () => {
    mockListRuns.mockResolvedValue(RUNS_FIXTURE_PAGE_1);
    const user = userEvent.setup();
    renderWithQueryClient(<RunsPage />);

    const table = await screen.findByRole("table");
    const firstRow = within(table).getAllByRole("link")[0]!;
    await user.click(firstRow);

    expect(push).toHaveBeenCalledWith(`/runs/${RUNS_FIXTURE_PAGE_1.items[0]!.run_id}`);
  });

  it("navigates on Enter for keyboard users", async () => {
    mockListRuns.mockResolvedValue(RUNS_FIXTURE_PAGE_1);
    const user = userEvent.setup();
    renderWithQueryClient(<RunsPage />);

    const table = await screen.findByRole("table");
    const firstRow = within(table).getAllByRole("link")[0]!;
    firstRow.focus();
    await user.keyboard("{Enter}");

    expect(push).toHaveBeenCalledWith(`/runs/${RUNS_FIXTURE_PAGE_1.items[0]!.run_id}`);
  });
});
