"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";

import type { TraceEventKind, TraceEventResource } from "@/lib/api/client";
import { createRunEventSource, TERMINAL_TRACE_EVENT_KINDS, TRACE_EVENT_KINDS } from "@/lib/sse";

export type SseConnectionStatus = "idle" | "connecting" | "open" | "reconnecting" | "disconnected";

/** Trace kinds that change what `RunResource` itself reports (status,
 * pending_approval, steps) — the trace stream never carries that resource,
 * so these are the moments to re-fetch it from the authoritative REST
 * endpoint instead of reconstructing it from trace payloads. */
const RUN_RESOURCE_INVALIDATING_KINDS: ReadonlySet<TraceEventKind> = new Set([
  ...TERMINAL_TRACE_EVENT_KINDS,
  "approval_requested",
  "approval_granted",
  "approval_rejected",
  "approval_expired",
  "approval_superseded",
  "run_recovered",
]);

/**
 * Live trace updates for one run over SSE (§13.4, FE-007) — an enhancement
 * over `useRunTrace`'s reload-reconstruction fetch, never a replacement for
 * it: this hook only appends newly-arrived, deduplicated events into the
 * same `["runs", runId, "trace"]` query cache `useRunTrace` populates, and
 * invalidates `["runs", runId]` so the run resource itself (status,
 * pending_approval) stays authoritative from the server.
 *
 * Disabled (`enabled: false`) once the caller already knows the run is
 * terminal — reconnecting to a stream that can never produce another event
 * would just be a reconnect loop with nothing to show for it.
 */
export function useRunEvents(
  runId: string,
  {
    enabled,
    createSource = createRunEventSource,
  }: { enabled: boolean; createSource?: (runId: string) => EventSource }
): SseConnectionStatus {
  const queryClient = useQueryClient();
  const [status, setStatus] = React.useState<SseConnectionStatus>("idle");

  React.useEffect(() => {
    if (!enabled || !runId) {
      setStatus("idle");
      return;
    }

    let closed = false;
    const source = createSource(runId);
    setStatus("connecting");

    source.onopen = () => {
      if (closed) return;
      setStatus("open");
    };

    source.onerror = () => {
      if (closed) return;
      // The browser's EventSource retries automatically (and replays via
      // Last-Event-ID, §13.4) unless we've closed it ourselves; readyState
      // is the only signal for which case this is.
      setStatus(source.readyState === EventSource.CONNECTING ? "reconnecting" : "disconnected");
    };

    function handleEvent(event: MessageEvent<string>) {
      if (closed) return;
      let resource: TraceEventResource;
      try {
        resource = JSON.parse(event.data) as TraceEventResource;
      } catch {
        return;
      }

      queryClient.setQueryData<TraceEventResource[]>(["runs", runId, "trace"], (current) => {
        const events = current ?? [];
        // Deduplicate by the backend's own event identity — `seq` is
        // monotonic per run (§13.4) — since a reconnect replays from
        // Last-Event-ID and can legitimately repeat the boundary event.
        if (events.some((e) => e.seq === resource.seq)) return events;
        return [...events, resource].sort((a, b) => a.seq - b.seq);
      });

      if (RUN_RESOURCE_INVALIDATING_KINDS.has(resource.kind)) {
        queryClient.invalidateQueries({ queryKey: ["runs", runId], exact: true });
      }

      if (TERMINAL_TRACE_EVENT_KINDS.has(resource.kind)) {
        // The run just ended: close deliberately rather than letting the
        // server-closed stream trigger the browser's automatic reconnect
        // into a run that can never produce another event.
        closed = true;
        source.close();
        setStatus("disconnected");
      }
    }

    for (const kind of TRACE_EVENT_KINDS) {
      source.addEventListener(kind, handleEvent as EventListener);
    }

    return () => {
      closed = true;
      for (const kind of TRACE_EVENT_KINDS) {
        source.removeEventListener(kind, handleEvent as EventListener);
      }
      source.close();
    };
  }, [runId, enabled, createSource, queryClient]);

  return status;
}
