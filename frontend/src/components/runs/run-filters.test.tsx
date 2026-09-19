import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { EMPTY_RUN_FILTERS, RunFilters, hasActiveFilters } from "@/components/runs/run-filters";

describe("hasActiveFilters", () => {
  it("is false for the empty filter set", () => {
    expect(hasActiveFilters(EMPTY_RUN_FILTERS)).toBe(false);
  });

  it("is true when any field is set", () => {
    expect(hasActiveFilters({ ...EMPTY_RUN_FILTERS, q: "fintech" })).toBe(true);
    expect(hasActiveFilters({ ...EMPTY_RUN_FILTERS, status: ["failed"] })).toBe(true);
  });
});

describe("RunFilters", () => {
  it("toggles a status pill on and off", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RunFilters value={EMPTY_RUN_FILTERS} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "failed" }));
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_RUN_FILTERS, status: ["failed"] });
  });

  it("removes an already-active status on a second click", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RunFilters value={{ ...EMPTY_RUN_FILTERS, status: ["failed"] }} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "failed" }));
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_RUN_FILTERS, status: [] });
  });

  it("updates the search text", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RunFilters value={EMPTY_RUN_FILTERS} onChange={onChange} />);

    await user.type(screen.getByLabelText("Search run request text"), "x");
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_RUN_FILTERS, q: "x" });
  });

  it("only shows Clear once a filter is active", () => {
    const { rerender } = render(<RunFilters value={EMPTY_RUN_FILTERS} onChange={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /clear/i })).not.toBeInTheDocument();

    rerender(<RunFilters value={{ ...EMPTY_RUN_FILTERS, q: "fintech" }} onChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: /clear/i })).toBeInTheDocument();
  });

  it("resets to the empty filter set when Clear is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <RunFilters value={{ ...EMPTY_RUN_FILTERS, q: "fintech", status: ["failed"] }} onChange={onChange} />
    );

    await user.click(screen.getByRole("button", { name: /clear/i }));
    expect(onChange).toHaveBeenCalledWith(EMPTY_RUN_FILTERS);
  });

  it("reveals since/until/parent run id fields behind the Filters toggle", async () => {
    const user = userEvent.setup();
    render(<RunFilters value={EMPTY_RUN_FILTERS} onChange={vi.fn()} />);

    expect(screen.queryByText("Parent run ID")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /filters/i }));
    expect(screen.getByText("Parent run ID")).toBeInTheDocument();
  });
});
