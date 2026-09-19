import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getRun, getRunTrace } from "@/lib/api/client";
import { buildFailedRunWithUnrunSteps, buildRunWithRetryAndApproval } from "@/test/fixtures/run-detail";
import { renderWithQueryClient } from "@/test/test-utils";

import { RunDetailView } from "./run-detail-view";

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, getRun: vi.fn(), getRunTrace: vi.fn() };
});

const mockGetRun = vi.mocked(getRun);
const mockGetRunTrace = vi.mocked(getRunTrace);

function mockRunAndTrace(run: ReturnType<typeof buildRunWithRetryAndApproval>["run"], events: ReturnType<typeof buildRunWithRetryAndApproval>["events"]) {
  mockGetRun.mockResolvedValue(run);
  mockGetRunTrace.mockResolvedValue({ run_id: run.run_id, events, next_seq: null, complete: true });
}

describe("RunDetailView", () => {
  beforeEach(() => {
    mockGetRun.mockReset();
    mockGetRunTrace.mockReset();
  });

  // --- loading -------------------------------------------------------------

  it("shows a loading skeleton before the run and trace arrive", () => {
    mockGetRun.mockImplementation(() => new Promise(() => {}));
    mockGetRunTrace.mockImplementation(() => new Promise(() => {}));
    renderWithQueryClient(<RunDetailView runId="8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b" />);

    expect(screen.queryByText(/Plan vs\. actual/)).not.toBeInTheDocument();
  });

  // --- not-found / error -------------------------------------------------------------

  it("shows a distinct not-found message for a 404", async () => {
    const { ApiError } = await import("@/lib/api/client");
    mockGetRun.mockRejectedValue(new ApiError(404, "Not Found", { code: "not_found" }));
    mockGetRunTrace.mockResolvedValue({ run_id: "x", events: [], next_seq: null, complete: true });
    renderWithQueryClient(<RunDetailView runId="missing-run" />);

    expect(await screen.findByText("Run not found")).toBeInTheDocument();
    expect(screen.getByText(/No run exists with id missing-run/)).toBeInTheDocument();
  });

  it("shows a generic error state for a non-404 failure", async () => {
    mockGetRun.mockRejectedValue(new Error("Failed to fetch"));
    mockGetRunTrace.mockResolvedValue({ run_id: "x", events: [], next_seq: null, complete: true });
    renderWithQueryClient(<RunDetailView runId="some-run" />);

    expect(await screen.findByText("Could not load this run")).toBeInTheDocument();
  });

  it("shows a trace-specific error without hiding the already-loaded run header", async () => {
    const { run } = buildRunWithRetryAndApproval();
    mockGetRun.mockResolvedValue(run);
    mockGetRunTrace.mockRejectedValue(new Error("trace fetch failed"));
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    expect(await screen.findByText("Could not load the execution trace")).toBeInTheDocument();
    expect(screen.getByText(run.user_request)).toBeInTheDocument();
  });

  // --- populated rendering -------------------------------------------------------------

  it("renders the run header with status, duration, step and retry counters", async () => {
    const { run, events } = buildFailedRunWithUnrunSteps();
    mockRunAndTrace(run, events);
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    expect(await screen.findByText(run.user_request)).toBeInTheDocument();
    // "budget_exhausted" also appears as a timeline marker under s2, so scope to the header.
    const header = screen.getByText(run.run_id).closest("[class*='rounded-lg']") as HTMLElement;
    expect(within(header).getByText("budget_exhausted")).toBeInTheDocument(); // status_reason
    expect(within(header).getByText("12.2s")).toBeInTheDocument(); // finished_at - started_at
    expect(within(header).getByText("Steps").parentElement).toHaveTextContent("2"); // step_count
    expect(within(header).getByText("Retries").parentElement).toHaveTextContent("2"); // retry_total
  });

  it("shows a live indicator instead of a settled duration for a run still in flight", async () => {
    const { run, events } = buildRunWithRetryAndApproval();
    mockRunAndTrace(run, events);
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    expect(await screen.findByText("running…")).toBeInTheDocument();
  });

  // --- plan vs actual + planned-but-unrun -------------------------------------------------------------

  it("renders every planned step and distinguishes unrun steps from executed ones", async () => {
    const { run, events } = buildRunWithRetryAndApproval();
    mockRunAndTrace(run, events);
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    const planSection = (await screen.findByText("Plan vs. actual")).closest("div")!.parentElement!;
    // All 7 planned steps show up, s6/s7 included even though they never ran.
    for (const id of ["s1", "s2", "s3", "s4", "s5", "s6", "s7"]) {
      expect(within(planSection).getByText(id)).toBeInTheDocument();
    }
    const unrun = within(planSection).getAllByText("Planned — not yet run");
    expect(unrun).toHaveLength(2); // s6 and s7
  });

  it("shows the retry count on a step that actually retried", async () => {
    const { run, events } = buildRunWithRetryAndApproval();
    mockRunAndTrace(run, events);
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    await screen.findByText("Plan vs. actual");
    expect(screen.getByText("(1 retry)")).toBeInTheDocument();
  });

  // --- nested retries (timeline) -------------------------------------------------------------

  it("nests both attempts of a retried step under one step group in the timeline", async () => {
    const { run, events } = buildRunWithRetryAndApproval();
    mockRunAndTrace(run, events);
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    await screen.findByText("Execution timeline");
    expect(screen.getByText("retry scheduled — waiting 250ms")).toBeInTheDocument();
    expect(screen.getAllByText(/attempt 1/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/attempt 2/).length).toBeGreaterThan(0);
  });

  // --- approval gap -------------------------------------------------------------

  it("renders a labelled, unresolved approval gap for a run paused mid-timeline", async () => {
    const { run, events } = buildRunWithRetryAndApproval();
    mockRunAndTrace(run, events);
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    expect(await screen.findByText("Paused for approval")).toBeInTheDocument();
    expect(screen.getByText("Still waiting on a human decision.")).toBeInTheDocument();
  });

  it("renders the pending-approval panel with a link to its approval detail page, and no approve/reject action", async () => {
    const { run, events } = buildRunWithRetryAndApproval();
    mockRunAndTrace(run, events);
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    const heading = await screen.findByText("Awaiting approval");
    const panel = heading.closest("[class*='rounded-lg']") as HTMLElement;
    expect(within(panel).getByText(run.pending_approval!.title)).toBeInTheDocument();
    const link = within(panel).getByRole("link", { name: /Review this approval/ });
    expect(link).toHaveAttribute("href", `/approvals/${run.pending_approval!.approval_id}`);
    // The panel itself never renders an approve/reject action (FE-004's job) —
    // scoped to the panel because the Plan-vs-Actual row for this step's
    // rationale text ("...the approved outreach email") would otherwise
    // false-positive-match an /approve/i query against the whole document.
    expect(within(panel).queryByRole("button", { name: /^approve$/i })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: /^reject$/i })).not.toBeInTheDocument();
  });

  // --- step inspector -------------------------------------------------------------

  it("opens the step inspector with input, output and verification checks on selection", async () => {
    const { run, events } = buildRunWithRetryAndApproval();
    mockRunAndTrace(run, events);
    const user = userEvent.setup();
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    await screen.findByText("Plan vs. actual");
    await user.click(screen.getAllByText("s2")[0]!);

    expect(await screen.findByText("Attempt 1")).toBeInTheDocument();
    expect(screen.getByText("Attempt 2")).toBeInTheDocument();
    // Both attempts' resolved input, plus attempt 2's output (which echoes
    // company_id back as part of the enrichment result) — 3 occurrences.
    expect(screen.getAllByText(/"company_id": "C-9"/)).toHaveLength(3);
    expect(screen.getByText(/"name": "Northbridge Analytics"/)).toBeInTheDocument(); // output
    expect(screen.getByText("confidence_in_range")).toBeInTheDocument(); // verification check name
  });

  it("shows a placeholder prompt before any step is selected, and closes on request", async () => {
    const { run, events } = buildRunWithRetryAndApproval();
    mockRunAndTrace(run, events);
    const user = userEvent.setup();
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    expect(await screen.findByText(/Select a step from the plan or the timeline/)).toBeInTheDocument();

    await user.click(screen.getAllByText("s1")[0]!);
    await waitFor(() => expect(screen.queryByText(/Select a step/)).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /close step inspector/i }));
    expect(await screen.findByText(/Select a step from the plan or the timeline/)).toBeInTheDocument();
  });

  it("shows an unrun step with no attempts in the inspector, inventing nothing", async () => {
    const { run, events } = buildRunWithRetryAndApproval();
    mockRunAndTrace(run, events);
    const user = userEvent.setup();
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);

    await screen.findByText("Plan vs. actual");
    await user.click(screen.getAllByText("s7")[0]!);

    expect(await screen.findByText(/has not run yet/)).toBeInTheDocument();
  });

  // --- reload / reconstruction -------------------------------------------------------------

  it("re-fetches the run and trace from the API on every mount — no client-only state to reconstruct from", async () => {
    const { run, events } = buildRunWithRetryAndApproval();
    mockRunAndTrace(run, events);
    const { unmount } = renderWithQueryClient(<RunDetailView runId={run.run_id} />);
    await screen.findByText("Plan vs. actual");
    expect(mockGetRun).toHaveBeenCalledTimes(1);
    expect(mockGetRunTrace).toHaveBeenCalledTimes(1);

    // Simulate a hard reload: unmount everything (including the QueryClient
    // this test's provider owns) and mount fresh, exactly as a browser
    // reload would produce a brand-new page with no prior React state.
    unmount();
    renderWithQueryClient(<RunDetailView runId={run.run_id} />);
    await screen.findByText("Plan vs. actual");

    expect(mockGetRun).toHaveBeenCalledTimes(2);
    expect(mockGetRunTrace).toHaveBeenCalledTimes(2);
  });
});
