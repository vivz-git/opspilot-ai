import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { listTools } from "@/lib/api/client";
import { TOOLS_FIXTURE } from "@/test/fixtures/tools";
import { renderWithQueryClient } from "@/test/test-utils";

import ToolsPage from "./page";

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, listTools: vi.fn() };
});

const mockListTools = vi.mocked(listTools);

describe("ToolsPage", () => {
  beforeEach(() => {
    mockListTools.mockReset();
  });

  // --- loading / empty / error ---------------------------------------------

  it("shows a loading skeleton before data arrives", () => {
    mockListTools.mockImplementation(() => new Promise(() => {}));
    renderWithQueryClient(<ToolsPage />);

    expect(screen.getByText("Tools")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows an empty state when the registry has no tools", async () => {
    mockListTools.mockResolvedValue([]);
    renderWithQueryClient(<ToolsPage />);

    expect(await screen.findByText("No tools are registered.")).toBeInTheDocument();
  });

  it("shows an error state on failure", async () => {
    mockListTools.mockRejectedValue(new Error("Failed to fetch"));
    renderWithQueryClient(<ToolsPage />);

    expect(await screen.findByText("Could not load the tool catalog")).toBeInTheDocument();
  });

  // --- populated rendering ---------------------------------------------------

  it("renders every registered tool with side effect and risk", async () => {
    mockListTools.mockResolvedValue(TOOLS_FIXTURE);
    renderWithQueryClient(<ToolsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("link");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("search_leads")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("read_only")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("send_email_mock")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("gated")).toBeInTheDocument();
  });

  it("never renders an execute/run affordance — the catalog is read-only", async () => {
    mockListTools.mockResolvedValue(TOOLS_FIXTURE);
    renderWithQueryClient(<ToolsPage />);

    await screen.findByRole("table");
    expect(screen.queryByRole("button", { name: /run|execute|invoke/i })).not.toBeInTheDocument();
  });

  // --- inspector -------------------------------------------------------------

  it("opens the inspector with schema, policy facts and failure modes", async () => {
    mockListTools.mockResolvedValue(TOOLS_FIXTURE);
    const user = userEvent.setup();
    renderWithQueryClient(<ToolsPage />);

    const table = await screen.findByRole("table");
    await user.click(within(table).getByText("search_leads").closest("tr")!);

    expect(await screen.findByText("Input schema")).toBeInTheDocument();
    expect(screen.getByText("Output schema")).toBeInTheDocument();
    expect(screen.getByText("arguments failed the input schema")).toBeInTheDocument();
    // The raw JSON schema is rendered as technical (monospace) text.
    expect(screen.getByText(/"query"/)).toBeInTheDocument();
  });

  it("closes the inspector", async () => {
    mockListTools.mockResolvedValue(TOOLS_FIXTURE);
    const user = userEvent.setup();
    renderWithQueryClient(<ToolsPage />);

    const table = await screen.findByRole("table");
    await user.click(within(table).getByText("search_leads").closest("tr")!);
    expect(await screen.findByLabelText("Close tool inspector")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Close tool inspector"));
    expect(screen.queryByLabelText("Close tool inspector")).not.toBeInTheDocument();
  });

  it("copies the input schema to the clipboard", async () => {
    mockListTools.mockResolvedValue(TOOLS_FIXTURE);
    // userEvent.setup() installs its own clipboard stub, so ours must be
    // defined after — otherwise setup() silently overwrites it.
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    renderWithQueryClient(<ToolsPage />);

    const table = await screen.findByRole("table");
    await user.click(within(table).getByText("search_leads").closest("tr")!);
    await user.click(await screen.findByLabelText("Copy Input schema JSON"));

    expect(writeText).toHaveBeenCalledWith(JSON.stringify(TOOLS_FIXTURE[0]!.schemas.input, null, 2));
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("is keyboard-activatable via Enter", async () => {
    mockListTools.mockResolvedValue(TOOLS_FIXTURE);
    renderWithQueryClient(<ToolsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("link");
    rows[0]!.focus();
    await userEvent.keyboard("{Enter}");

    expect(await screen.findByText("Input schema")).toBeInTheDocument();
  });
});
