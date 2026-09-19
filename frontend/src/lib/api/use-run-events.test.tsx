import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { TraceEventResource } from "@/lib/api/client";

import { useRunEvents } from "./use-run-events";

const RUN_ID = "8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b";

/** A minimal, fully controllable stand-in for the browser's `EventSource` —
 * lets each test drive open/error/message deterministically instead of
 * depending on real network timing. */
class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSED = 2;
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closeSpy = vi.fn(() => {
    this.readyState = FakeEventSource.CLOSED;
  });

  constructor(public url: string) {
    super();
  }

  open() {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }

  errorOut(readyState: number) {
    this.readyState = readyState;
    this.onerror?.();
  }

  emit(kind: string, resource: Partial<TraceEventResource>) {
    this.dispatchEvent(new MessageEvent(kind, { data: JSON.stringify(resource) }));
  }

  close() {
    this.closeSpy();
  }
}

function event(seq: number, kind: string, overrides: Partial<TraceEventResource> = {}): TraceEventResource {
  return {
    seq,
    ts: "2026-09-19T08:12:00Z",
    kind: kind as TraceEventResource["kind"],
    severity: "info",
    node: null,
    tool: null,
    step_id: null,
    attempt: null,
    status: null,
    duration_ms: null,
    retry_count: null,
    error: null,
    payload: {},
    input: null,
    output: null,
    ...overrides,
  };
}

function setup() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const sources: FakeEventSource[] = [];
  const createSource = vi.fn((runId: string) => {
    const source = new FakeEventSource(`http://test/runs/${runId}/events`);
    sources.push(source);
    return source as unknown as EventSource;
  });
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { queryClient, sources, createSource, wrapper };
}

describe("useRunEvents", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  // --- connection lifecycle -------------------------------------------------

  it("stays idle and opens no connection when disabled", () => {
    const { createSource, wrapper } = setup();
    const { result } = renderHook(() => useRunEvents(RUN_ID, { enabled: false, createSource }), {
      wrapper,
    });

    expect(result.current).toBe("idle");
    expect(createSource).not.toHaveBeenCalled();
  });

  it("transitions connecting -> open as the connection establishes", async () => {
    const { sources, createSource, wrapper } = setup();
    const { result } = renderHook(() => useRunEvents(RUN_ID, { enabled: true, createSource }), {
      wrapper,
    });

    expect(result.current).toBe("connecting");
    sources[0]!.open();
    await waitFor(() => expect(result.current).toBe("open"));
  });

  // --- incoming events ---------------------------------------------------

  it("merges an incoming event into the trace query cache", async () => {
    const { sources, createSource, wrapper, queryClient } = setup();
    renderHook(() => useRunEvents(RUN_ID, { enabled: true, createSource }), { wrapper });

    sources[0]!.emit("tool_started", event(5, "tool_started"));

    await waitFor(() => {
      const cached = queryClient.getQueryData<TraceEventResource[]>(["runs", RUN_ID, "trace"]);
      expect(cached).toHaveLength(1);
      expect(cached?.[0]?.seq).toBe(5);
    });
  });

  it("deduplicates by seq — a replayed event on reconnect is not added twice", async () => {
    const { sources, createSource, wrapper, queryClient } = setup();
    renderHook(() => useRunEvents(RUN_ID, { enabled: true, createSource }), { wrapper });

    sources[0]!.emit("tool_started", event(5, "tool_started"));
    sources[0]!.emit("tool_started", event(5, "tool_started"));

    await waitFor(() => {
      const cached = queryClient.getQueryData<TraceEventResource[]>(["runs", RUN_ID, "trace"]);
      expect(cached).toHaveLength(1);
    });
  });

  it("preserves ascending seq order regardless of arrival order", async () => {
    const { sources, createSource, wrapper, queryClient } = setup();
    renderHook(() => useRunEvents(RUN_ID, { enabled: true, createSource }), { wrapper });

    sources[0]!.emit("tool_succeeded", event(3, "tool_succeeded"));
    sources[0]!.emit("tool_started", event(1, "tool_started"));
    sources[0]!.emit("tool_started", event(2, "tool_started"));

    await waitFor(() => {
      const cached = queryClient.getQueryData<TraceEventResource[]>(["runs", RUN_ID, "trace"]);
      expect(cached?.map((e) => e.seq)).toEqual([1, 2, 3]);
    });
  });

  it("invalidates the run resource query on an approval event, without mutating it locally", async () => {
    const { sources, createSource, wrapper, queryClient } = setup();
    queryClient.setQueryData(["runs", RUN_ID], { run_id: RUN_ID, status: "awaiting_approval" });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    renderHook(() => useRunEvents(RUN_ID, { enabled: true, createSource }), { wrapper });

    sources[0]!.emit("approval_requested", event(4, "approval_requested"));

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["runs", RUN_ID], exact: true });
    });
    // The hook never writes to the run query itself — only the REST refetch does.
    expect(queryClient.getQueryData(["runs", RUN_ID])).toEqual({
      run_id: RUN_ID,
      status: "awaiting_approval",
    });
  });

  // --- reconnect / failure -------------------------------------------------

  it("reports 'reconnecting' while the browser retries, distinct from a hard close", async () => {
    const { sources, createSource, wrapper } = setup();
    const { result } = renderHook(() => useRunEvents(RUN_ID, { enabled: true, createSource }), {
      wrapper,
    });

    sources[0]!.open();
    await waitFor(() => expect(result.current).toBe("open"));

    sources[0]!.errorOut(FakeEventSource.CONNECTING);
    await waitFor(() => expect(result.current).toBe("reconnecting"));
  });

  it("reports 'disconnected' on a stream failure that will not retry", async () => {
    const { sources, createSource, wrapper } = setup();
    const { result } = renderHook(() => useRunEvents(RUN_ID, { enabled: true, createSource }), {
      wrapper,
    });

    sources[0]!.errorOut(FakeEventSource.CLOSED);
    await waitFor(() => expect(result.current).toBe("disconnected"));
  });

  // --- terminal run behavior -----------------------------------------------

  it("closes the connection itself on a terminal trace event, instead of letting the browser retry", async () => {
    const { sources, createSource, wrapper } = setup();
    const { result } = renderHook(() => useRunEvents(RUN_ID, { enabled: true, createSource }), {
      wrapper,
    });

    sources[0]!.emit("run_completed", event(9, "run_completed"));

    await waitFor(() => expect(result.current).toBe("disconnected"));
    expect(sources[0]!.closeSpy).toHaveBeenCalled();
  });

  // --- cleanup / unmount ---------------------------------------------------

  it("closes the connection on unmount", () => {
    const { sources, createSource, wrapper } = setup();
    const { unmount } = renderHook(() => useRunEvents(RUN_ID, { enabled: true, createSource }), {
      wrapper,
    });

    unmount();
    expect(sources[0]!.closeSpy).toHaveBeenCalledTimes(1);
  });

  it("ignores events that arrive after unmount", async () => {
    const { sources, createSource, wrapper, queryClient } = setup();
    const { unmount } = renderHook(() => useRunEvents(RUN_ID, { enabled: true, createSource }), {
      wrapper,
    });
    const source = sources[0]!;
    unmount();

    expect(() => source.emit("tool_started", event(1, "tool_started"))).not.toThrow();
    expect(queryClient.getQueryData(["runs", RUN_ID, "trace"])).toBeUndefined();
  });

  it("opens a fresh connection when runId changes and closes the old one", () => {
    const { sources, createSource, wrapper } = setup();
    const { rerender } = renderHook(({ id }) => useRunEvents(id, { enabled: true, createSource }), {
      wrapper,
      initialProps: { id: RUN_ID },
    });

    rerender({ id: "a-different-run-id" });

    expect(sources[0]!.closeSpy).toHaveBeenCalled();
    expect(sources).toHaveLength(2);
    expect(sources[1]!.url).toContain("a-different-run-id");
  });
});
