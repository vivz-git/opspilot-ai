import { describe, expect, it } from "vitest";

import { buildPlanVsActual, parsePlannedSteps } from "@/lib/plan";
import { buildFailedRunWithUnrunSteps, buildRunWithRetryAndApproval } from "@/test/fixtures/run-detail";

describe("parsePlannedSteps", () => {
  it("returns an empty list for a null plan", () => {
    expect(parsePlannedSteps(null)).toEqual([]);
  });

  it("ignores malformed entries without a step_id or tool", () => {
    const steps = parsePlannedSteps({ steps: [{ step_id: "s1" }, { tool: "x" }, "not-an-object"] });
    expect(steps).toEqual([]);
  });

  it("parses a well-formed plan", () => {
    const { run } = buildRunWithRetryAndApproval();
    const steps = parsePlannedSteps(run.plan);
    expect(steps.map((s) => s.step_id)).toEqual(["s1", "s2", "s3", "s4", "s5", "s6", "s7"]);
    expect(steps[0]).toMatchObject({ tool: "search_leads", optional: false, fanout: null });
  });
});

describe("buildPlanVsActual", () => {
  it("marks steps with a matching actual entry as ran, and later steps as not", () => {
    const { run } = buildRunWithRetryAndApproval();
    const planned = parsePlannedSteps(run.plan);
    const { rows, unplanned } = buildPlanVsActual(planned, run.steps ?? []);

    const byId = Object.fromEntries(rows.map((r) => [r.step.step_id, r]));
    expect(byId.s1!.ran).toBe(true);
    expect(byId.s5!.ran).toBe(true);
    // Paused at approval before s6 executed, s7 never reached.
    expect(byId.s6!.ran).toBe(false);
    expect(byId.s6!.actual).toBeNull();
    expect(byId.s7!.ran).toBe(false);
    expect(unplanned).toEqual([]);
  });

  it("carries the retry count through to the actual step row", () => {
    const { run } = buildRunWithRetryAndApproval();
    const planned = parsePlannedSteps(run.plan);
    const { rows } = buildPlanVsActual(planned, run.steps ?? []);
    const s2 = rows.find((r) => r.step.step_id === "s2")!;
    expect(s2.actual?.attempts).toBe(2);
    expect(s2.actual?.retry_count).toBe(1);
  });

  it("does not invent an execution result for a step that never ran", () => {
    const { run } = buildFailedRunWithUnrunSteps();
    const planned = parsePlannedSteps(run.plan);
    const { rows } = buildPlanVsActual(planned, run.steps ?? []);
    const unrun = rows.filter((r) => !r.ran);
    expect(unrun.map((r) => r.step.step_id)).toEqual(["s3", "s4", "s5", "s6", "s7"]);
    for (const row of unrun) {
      expect(row.actual).toBeNull();
    }
  });

  it("expands a fan-out template against its concrete children", () => {
    const planned = parsePlannedSteps({
      steps: [{ step_id: "s2", tool: "research_company", fanout: { over: "s1.output.leads", as: "lead", max_items: 10 } }],
    });
    const actual = [
      { step_id: "s2[0]", tool: "research_company", seq: 1, status: "succeeded", attempts: 1, retry_count: 0, verification_status: "passed", duration_ms: 100, error: null },
      { step_id: "s2[1]", tool: "research_company", seq: 2, status: "succeeded", attempts: 1, retry_count: 0, verification_status: "passed", duration_ms: 90, error: null },
    ];
    const { rows, unplanned } = buildPlanVsActual(planned, actual);
    expect(rows[0]!.ran).toBe(true);
    expect(rows[0]!.fanoutChildren.map((c) => c.step_id)).toEqual(["s2[0]", "s2[1]"]);
    expect(unplanned).toEqual([]);
  });

  it("surfaces actual steps that don't map to the current plan as unplanned, never dropping them", () => {
    const planned = parsePlannedSteps({ steps: [{ step_id: "s1", tool: "search_leads" }] });
    const actual = [
      { step_id: "s1", tool: "search_leads", seq: 1, status: "succeeded", attempts: 1, retry_count: 0, verification_status: "passed", duration_ms: 100, error: null },
      { step_id: "s0-old-revision", tool: "search_leads", seq: 0, status: "succeeded", attempts: 1, retry_count: 0, verification_status: "passed", duration_ms: 80, error: null },
    ];
    const { unplanned } = buildPlanVsActual(planned, actual);
    expect(unplanned.map((s) => s.step_id)).toEqual(["s0-old-revision"]);
  });
});
