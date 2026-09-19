import { describe, expect, it } from "vitest";

import { buildTimeline, eventsForStep, groupStepBlocks } from "@/lib/trace";
import { buildFailedRunWithUnrunSteps, buildRunWithRetryAndApproval } from "@/test/fixtures/run-detail";

describe("groupStepBlocks / nested retries", () => {
  it("nests a retried step into ordered attempt blocks with input/output preserved", () => {
    const { events } = buildRunWithRetryAndApproval();
    const s2Events = eventsForStep(events, "s2");
    const blocks = groupStepBlocks(s2Events);

    const attemptBlocks = blocks.filter((b) => b.kind === "attempt");
    expect(attemptBlocks.map((b) => b.attempt)).toEqual([1, 2]);

    const [attempt1, attempt2] = attemptBlocks;
    expect(attempt1!.started?.input).toEqual({ company_id: "C-9" });
    expect(attempt1!.closed?.kind).toBe("tool_failed");
    expect(attempt1!.closed?.error).toMatchObject({ class: "transient" });
    expect(attempt2!.closed?.kind).toBe("tool_succeeded");
    expect(attempt2!.closed?.output).toMatchObject({ name: "Northbridge Analytics" });
    expect(attempt2!.verifications).toHaveLength(1);

    // The retry marker sits between the two attempts, not folded into either.
    const retryMarker = blocks.find((b) => b.kind === "event" && b.event.kind === "retry_scheduled");
    expect(retryMarker).toBeDefined();
    const order = blocks.map((b) => (b.kind === "attempt" ? `attempt:${b.attempt}` : b.event.kind));
    expect(order.indexOf("attempt:1")).toBeLessThan(order.indexOf("retry_scheduled"));
    expect(order.indexOf("retry_scheduled")).toBeLessThan(order.indexOf("attempt:2"));
  });

  it("nests three attempts for a step that exhausted its retry budget", () => {
    const { events } = buildFailedRunWithUnrunSteps();
    const blocks = groupStepBlocks(eventsForStep(events, "s2"));
    const attempts = blocks.filter((b) => b.kind === "attempt");
    expect(attempts.map((b) => b.attempt)).toEqual([1, 2, 3]);
    expect(attempts.every((b) => b.closed?.kind === "tool_failed")).toBe(true);
    const retryMarkers = blocks.filter((b) => b.kind === "event" && b.event.kind === "retry_scheduled");
    expect(retryMarkers).toHaveLength(2); // one after attempt 1, one after attempt 2 — none after attempt 3 (budget exhausted)
  });
});

describe("buildTimeline / approval gap", () => {
  it("opens a labelled, unresolved approval gap for a run currently paused", () => {
    const { events } = buildRunWithRetryAndApproval();
    const timeline = buildTimeline(events);
    const gap = timeline.find((e) => e.kind === "approval-gap");
    if (gap?.kind !== "approval-gap") throw new Error("expected an approval-gap entry");
    expect(gap).toMatchObject({ stepId: "s6", resolved: null, waitMs: null });
    expect(gap.requested.kind).toBe("approval_requested");
  });

  it("computes the wait duration for a resolved approval gap", () => {
    const { events } = buildRunWithRetryAndApproval();
    const requested = events.find((e) => e.kind === "approval_requested")!;
    const granted = {
      ...requested,
      seq: requested.seq + 1,
      kind: "approval_granted" as const,
      ts: new Date(new Date(requested.ts).getTime() + 45_000).toISOString(),
      payload: { approval_id: (requested.payload as { approval_id: string }).approval_id, decision: "approve" },
    };
    const timeline = buildTimeline([...events, granted]);
    const gap = timeline.find((e) => e.kind === "approval-gap");
    expect(gap).toMatchObject({ resolved: granted, waitMs: 45_000 });
    // The resolution event is folded into the gap, not also rendered as its own row.
    expect(timeline.some((e) => e.kind === "marker" && e.event.kind === "approval_granted")).toBe(false);
  });

  it("hides node_entered/node_exited noise from the rendered timeline", () => {
    const { events } = buildRunWithRetryAndApproval();
    const withNodeEvents = [
      ...events,
      { ...events[0]!, seq: 999, kind: "node_entered" as const, node: "plan" },
    ];
    const timeline = buildTimeline(withNodeEvents);
    expect(timeline.some((e) => e.kind === "marker" && e.event.kind === "node_entered")).toBe(false);
  });

  it("groups every step-scoped event under one step entry, in first-seen order", () => {
    const { events } = buildRunWithRetryAndApproval();
    const timeline = buildTimeline(events);
    const stepEntries = timeline.filter((e) => e.kind === "step");
    expect(stepEntries.map((e) => e.kind === "step" && e.stepId)).toEqual(["s1", "s2", "s3", "s4", "s5"]);
  });

  it("orders run-level markers and step groups by seq", () => {
    const { events } = buildRunWithRetryAndApproval();
    const timeline = buildTimeline(events);
    const seqs = timeline.map((e) => e.seq);
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
    const first = timeline[0];
    if (first?.kind !== "marker") throw new Error("expected the first entry to be a marker");
    expect(first.event.kind).toBe("run_created");
  });
});
