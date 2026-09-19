import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getEvaluationRun, listEvaluationResults } from "@/lib/api/client";
import {
  EVALUATION_RESULTS_FIXTURE,
  makeEvaluationResult,
  makeEvaluationRun,
} from "@/test/fixtures/evaluations";
import { renderWithQueryClient } from "@/test/test-utils";

import { EvaluationRunDetailView } from "./evaluation-run-detail-view";

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, getEvaluationRun: vi.fn(), listEvaluationResults: vi.fn() };
});

const mockGetRun = vi.mocked(getEvaluationRun);
const mockListResults = vi.mocked(listEvaluationResults);

describe("EvaluationRunDetailView", () => {
  beforeEach(() => {
    mockGetRun.mockReset();
    mockListResults.mockReset();
  });

  it("shows a loading skeleton before the run arrives", () => {
    mockGetRun.mockImplementation(() => new Promise(() => {}));
    mockListResults.mockImplementation(() => new Promise(() => {}));
    renderWithQueryClient(<EvaluationRunDetailView evaluationRunId="e1" />);

    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows a distinct not-found message for a 404", async () => {
    const { ApiError } = await import("@/lib/api/client");
    mockGetRun.mockRejectedValue(new ApiError(404, "Not Found", { code: "not_found" }));
    mockListResults.mockResolvedValue([]);
    renderWithQueryClient(<EvaluationRunDetailView evaluationRunId="missing" />);

    expect(await screen.findByText("Evaluation run not found")).toBeInTheDocument();
    expect(screen.getByText(/No evaluation run exists with id missing/)).toBeInTheDocument();
  });

  it("shows a generic error state for a non-404 failure", async () => {
    mockGetRun.mockRejectedValue(new Error("Failed to fetch"));
    mockListResults.mockResolvedValue([]);
    renderWithQueryClient(<EvaluationRunDetailView evaluationRunId="e1" />);

    expect(await screen.findByText("Could not load this evaluation run")).toBeInTheDocument();
  });

  it("renders the run header, metrics and case table", async () => {
    const run = makeEvaluationRun();
    mockGetRun.mockResolvedValue(run);
    mockListResults.mockResolvedValue(EVALUATION_RESULTS_FIXTURE);
    renderWithQueryClient(<EvaluationRunDetailView evaluationRunId={run.evaluation_run_id} />);

    expect(await screen.findByText(run.suite)).toBeInTheDocument();
    expect(screen.getByText("Run metrics")).toBeInTheDocument();

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("link");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("happy_path_multi_step")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("invalid_tool_result")).toBeInTheDocument();
  });

  it("shows an empty-results hint distinct from a running suite", async () => {
    const run = makeEvaluationRun({ status: "running" });
    mockGetRun.mockResolvedValue(run);
    mockListResults.mockResolvedValue([]);
    renderWithQueryClient(<EvaluationRunDetailView evaluationRunId={run.evaluation_run_id} />);

    expect(await screen.findByText(/still running/)).toBeInTheDocument();
  });

  it("distinguishes passed and failed cases in the table", async () => {
    const run = makeEvaluationRun();
    mockGetRun.mockResolvedValue(run);
    mockListResults.mockResolvedValue(EVALUATION_RESULTS_FIXTURE);
    renderWithQueryClient(<EvaluationRunDetailView evaluationRunId={run.evaluation_run_id} />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("passed")).toBeInTheDocument();
    expect(within(table).getByText("failed")).toBeInTheDocument();
  });

  it("opens the case inspector with failed assertion evidence, not fabricated pass evidence", async () => {
    const run = makeEvaluationRun();
    mockGetRun.mockResolvedValue(run);
    mockListResults.mockResolvedValue(EVALUATION_RESULTS_FIXTURE);
    const user = userEvent.setup();
    renderWithQueryClient(<EvaluationRunDetailView evaluationRunId={run.evaluation_run_id} />);

    const table = await screen.findByRole("table");
    const failedRow = within(table).getByText("invalid_tool_result").closest("tr")!;
    await user.click(failedRow);

    expect(await screen.findByText(/Failed assertions/)).toBeInTheDocument();
    expect(screen.getByText("invariant[3] tool_call_matches_step")).toBeInTheDocument();
    expect(screen.getByText("expected 1 row, found 0")).toBeInTheDocument();
    expect(screen.getByText(/assertion failed: output_validation/)).toBeInTheDocument();
  });

  it("closes the case inspector", async () => {
    const run = makeEvaluationRun();
    mockGetRun.mockResolvedValue(run);
    mockListResults.mockResolvedValue(EVALUATION_RESULTS_FIXTURE);
    const user = userEvent.setup();
    renderWithQueryClient(<EvaluationRunDetailView evaluationRunId={run.evaluation_run_id} />);

    const table = await screen.findByRole("table");
    await user.click(within(table).getByText("happy_path_multi_step").closest("tr")!);
    expect(await screen.findByLabelText("Close case inspector")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Close case inspector"));
    expect(screen.queryByLabelText("Close case inspector")).not.toBeInTheDocument();
  });

  it("shows no-evidence copy when a passed case has no assertions recorded", async () => {
    const run = makeEvaluationRun();
    mockGetRun.mockResolvedValue(run);
    mockListResults.mockResolvedValue([makeEvaluationResult({ assertions: [] })]);
    const user = userEvent.setup();
    renderWithQueryClient(<EvaluationRunDetailView evaluationRunId={run.evaluation_run_id} />);

    const table = await screen.findByRole("table");
    await user.click(within(table).getAllByRole("link")[0]!);

    expect(await screen.findByText("No assertion evidence recorded.")).toBeInTheDocument();
  });
});
