import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createEvaluationRun, getEvaluationMetrics, listEvaluationRuns } from "@/lib/api/client";
import { EVALUATION_RUNS_FIXTURE, makeEvaluationRun } from "@/test/fixtures/evaluations";
import { renderWithQueryClient } from "@/test/test-utils";

import EvaluationsPage from "./page";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return {
    ...actual,
    listEvaluationRuns: vi.fn(),
    getEvaluationMetrics: vi.fn(),
    createEvaluationRun: vi.fn(),
  };
});

const mockListRuns = vi.mocked(listEvaluationRuns);
const mockGetMetrics = vi.mocked(getEvaluationMetrics);
const mockCreateRun = vi.mocked(createEvaluationRun);

describe("EvaluationsPage", () => {
  beforeEach(() => {
    push.mockClear();
    mockListRuns.mockReset();
    mockGetMetrics.mockReset();
    mockCreateRun.mockReset();
    mockGetMetrics.mockResolvedValue({ evaluation_run_id: null, suite: null, metrics: {} });
  });

  // --- loading / empty / error ---------------------------------------------

  it("shows a loading skeleton before data arrives", () => {
    mockListRuns.mockImplementation(() => new Promise(() => {}));
    renderWithQueryClient(<EvaluationsPage />);

    expect(screen.getByText("Evaluations")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows an empty state with no runs", async () => {
    mockListRuns.mockResolvedValue({ items: [] });
    renderWithQueryClient(<EvaluationsPage />);

    expect(await screen.findByText(/No evaluation runs yet/)).toBeInTheDocument();
  });

  it("shows an error state with a retry hint on failure", async () => {
    mockListRuns.mockRejectedValue(new Error("Failed to fetch"));
    renderWithQueryClient(<EvaluationsPage />);

    expect(await screen.findByText("Could not load evaluation runs")).toBeInTheDocument();
  });

  // --- populated rendering ---------------------------------------------------

  it("renders runs with suite, counts and status", async () => {
    mockListRuns.mockResolvedValue(EVALUATION_RUNS_FIXTURE);
    renderWithQueryClient(<EvaluationsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("link");
    expect(rows).toHaveLength(2);

    const firstRow = rows[0]!;
    expect(within(firstRow).getByText("all")).toBeInTheDocument();
    expect(within(firstRow).getByText("6")).toBeInTheDocument(); // passed
    expect(within(firstRow).getByText("1")).toBeInTheDocument(); // failed

    const runningRow = rows[1]!;
    expect(within(runningRow).getByText("running…")).toBeInTheDocument();
  });

  it("distinguishes running from terminal suites via the status badge", async () => {
    mockListRuns.mockResolvedValue(EVALUATION_RUNS_FIXTURE);
    renderWithQueryClient(<EvaluationsPage />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("completed")).toBeInTheDocument();
    expect(within(table).getByText("running")).toBeInTheDocument();
  });

  it("navigates to the run detail page on row activation", async () => {
    mockListRuns.mockResolvedValue(EVALUATION_RUNS_FIXTURE);
    const user = userEvent.setup();
    renderWithQueryClient(<EvaluationsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("link");
    await user.click(rows[0]!);

    expect(push).toHaveBeenCalledWith(
      `/evaluations/${EVALUATION_RUNS_FIXTURE.items[0]!.evaluation_run_id}`
    );
  });

  it("is keyboard-activatable via Enter", async () => {
    mockListRuns.mockResolvedValue(EVALUATION_RUNS_FIXTURE);
    renderWithQueryClient(<EvaluationsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("link");
    rows[0]!.focus();
    await userEvent.keyboard("{Enter}");

    expect(push).toHaveBeenCalledWith(
      `/evaluations/${EVALUATION_RUNS_FIXTURE.items[0]!.evaluation_run_id}`
    );
  });

  // --- trigger control ---------------------------------------------------

  it("starts a suite run and navigates to its detail page", async () => {
    mockListRuns.mockResolvedValue({ items: [] });
    const created = makeEvaluationRun({ evaluation_run_id: "new-run-id", status: "running" });
    mockCreateRun.mockResolvedValue(created);
    const user = userEvent.setup();
    renderWithQueryClient(<EvaluationsPage />);

    await screen.findByText(/No evaluation runs yet/);
    await user.click(screen.getByRole("button", { name: /run suite/i }));

    await waitFor(() => expect(mockCreateRun).toHaveBeenCalledWith({ suite: "all" }));
    expect(push).toHaveBeenCalledWith("/evaluations/new-run-id");
  });

  it("shows the metrics panel evidence, not a fabricated 100%", async () => {
    mockListRuns.mockResolvedValue(EVALUATION_RUNS_FIXTURE);
    mockGetMetrics.mockResolvedValue({
      evaluation_run_id: EVALUATION_RUNS_FIXTURE.items[0]!.evaluation_run_id,
      suite: "all",
      metrics: { case_pass_rate: 0.857, total_cases: 7 },
    });
    renderWithQueryClient(<EvaluationsPage />);

    const metricsPanel = (await screen.findByText("Metrics")).closest("[class*='rounded-lg']") as HTMLElement;
    expect(within(metricsPanel).getByText("86%")).toBeInTheDocument();
  });
});
