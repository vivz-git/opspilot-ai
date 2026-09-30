import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, createRun } from "@/lib/api/client";
import { CANONICAL_REQUEST } from "@/lib/runs";
import { buildRunWithRetryAndApproval } from "@/test/fixtures/run-detail";
import { renderWithQueryClient } from "@/test/test-utils";

import { NewRunForm } from "./new-run-form";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, createRun: vi.fn() };
});

const mockCreateRun = vi.mocked(createRun);

function textbox() {
  return screen.getByLabelText("New run");
}

function submitButton() {
  return screen.getByRole("button", { name: /Submit run|Submitting/ });
}

describe("NewRunForm", () => {
  beforeEach(() => {
    push.mockClear();
    mockCreateRun.mockReset();
  });

  it("cannot submit an empty or whitespace-only request", async () => {
    const user = userEvent.setup();
    renderWithQueryClient(<NewRunForm />);

    expect(submitButton()).toBeDisabled();
    await user.type(textbox(), "   ");
    expect(submitButton()).toBeDisabled();
    expect(mockCreateRun).not.toHaveBeenCalled();
  });

  it("fills the canonical request on demand", async () => {
    const user = userEvent.setup();
    renderWithQueryClient(<NewRunForm />);

    await user.click(screen.getByRole("button", { name: "Use the canonical request" }));
    expect(textbox()).toHaveValue(CANONICAL_REQUEST);
    expect(submitButton()).toBeEnabled();
  });

  it("submits to POST /runs with auto_start and opens the run the server created", async () => {
    const { run } = buildRunWithRetryAndApproval();
    let resolve: (value: typeof run) => void = () => {};
    mockCreateRun.mockImplementation(() => new Promise((r) => (resolve = r)));
    const user = userEvent.setup();
    renderWithQueryClient(<NewRunForm />);

    await user.click(screen.getByRole("button", { name: "Use the canonical request" }));
    await user.click(submitButton());

    expect(mockCreateRun).toHaveBeenCalledWith({
      user_request: CANONICAL_REQUEST,
      auto_start: true,
    });
    // Loading: the action is disabled and says so; nothing is shown as a run yet.
    expect(screen.getByRole("button", { name: "Submitting…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Submitting…" })).toHaveAttribute(
      "aria-busy",
      "true"
    );
    expect(textbox()).toBeDisabled();
    expect(push).not.toHaveBeenCalled();

    resolve(run);
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/runs/${run.run_id}`));
  });

  it("submits with Ctrl+Enter from the request box", async () => {
    const { run } = buildRunWithRetryAndApproval();
    mockCreateRun.mockResolvedValue(run);
    const user = userEvent.setup();
    renderWithQueryClient(<NewRunForm />);

    await user.type(textbox(), "Research company comp_northwind.");
    await user.keyboard("{Control>}{Enter}{/Control}");

    await waitFor(() => expect(push).toHaveBeenCalledWith(`/runs/${run.run_id}`));
    expect(mockCreateRun).toHaveBeenCalledTimes(1);
  });

  it("shows the server's validation error with its trace_id and does not navigate", async () => {
    mockCreateRun.mockRejectedValue(
      new ApiError(422, "Unprocessable Entity", {
        code: "validation_error",
        status: 422,
        detail: "Request body or query parameters failed schema validation",
        errors: [{ loc: ["body", "user_request"], msg: "user_request cannot be empty", type: "x" }],
        trace_id: "0123456789abcdef0123456789abcdef",
      })
    );
    const user = userEvent.setup();
    renderWithQueryClient(<NewRunForm />);

    await user.type(textbox(), "Do the thing");
    await user.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The request was rejected: user_request cannot be empty.");
    expect(alert).toHaveTextContent("trace_id 0123456789abcdef0123456789abcdef");
    expect(push).not.toHaveBeenCalled();
    // The request is kept and can be corrected and resubmitted.
    expect(textbox()).toHaveValue("Do the thing");
    expect(submitButton()).toBeEnabled();
  });

  it("falls back to the server's detail for codes it has no copy for", async () => {
    mockCreateRun.mockRejectedValue(
      new ApiError(500, "Internal Server Error", {
        code: "internal_error",
        detail: "An unexpected internal error occurred",
        trace_id: "fedcba9876543210fedcba9876543210",
      })
    );
    const user = userEvent.setup();
    renderWithQueryClient(<NewRunForm />);

    await user.type(textbox(), "Anything");
    await user.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("An unexpected internal error occurred");
    expect(alert).toHaveTextContent("trace_id fedcba9876543210fedcba9876543210");
  });

  it("explains an unreachable API without inventing a run", async () => {
    mockCreateRun.mockRejectedValue(new TypeError("Failed to fetch"));
    const user = userEvent.setup();
    renderWithQueryClient(<NewRunForm />);

    await user.type(textbox(), "Anything");
    await user.click(submitButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to fetch.*reachable/);
    expect(push).not.toHaveBeenCalled();
  });
});
