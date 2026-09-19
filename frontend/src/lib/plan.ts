import type { RunResource, RunStepSummary } from "@/lib/api/client";

/**
 * `RunResource.plan` is typed `{ [key: string]: unknown } | null` by the
 * generated schema — the backend itself treats it as opaque JSON (it is
 * never validated against a response model on the API layer, only against
 * `app.agent.state.Plan` internally). This describes that JSON's known
 * runtime shape (architecture.md §4.3 / `app/agent/state.py::PlanStep`) so
 * the plan can be rendered; it is not a hand-written replacement for a
 * generated response type, and every field is read defensively.
 */
export interface PlannedStep {
  step_id: string;
  tool: string;
  depends_on: string[];
  rationale: string;
  optional: boolean;
  fanout: { over: string; as: string; max_items: number } | null;
}

function asPlannedStep(value: unknown): PlannedStep | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as Record<string, unknown>;
  if (typeof v.step_id !== "string" || typeof v.tool !== "string") return null;

  const fanoutRaw = v.fanout;
  let fanout: PlannedStep["fanout"] = null;
  if (typeof fanoutRaw === "object" && fanoutRaw !== null) {
    const f = fanoutRaw as Record<string, unknown>;
    if (typeof f.over === "string" && typeof f.as === "string" && typeof f.max_items === "number") {
      fanout = { over: f.over, as: f.as, max_items: f.max_items };
    }
  }

  return {
    step_id: v.step_id,
    tool: v.tool,
    depends_on: Array.isArray(v.depends_on) ? v.depends_on.filter((d) => typeof d === "string") : [],
    rationale: typeof v.rationale === "string" ? v.rationale : "",
    optional: v.optional === true,
    fanout,
  };
}

/** Extracts the planned step list from a run's opaque `plan` JSON, or `[]` if absent/malformed. */
export function parsePlannedSteps(plan: RunResource["plan"]): PlannedStep[] {
  if (!plan) return [];
  const steps = (plan as Record<string, unknown>).steps;
  if (!Array.isArray(steps)) return [];
  return steps.map(asPlannedStep).filter((s): s is PlannedStep => s !== null);
}

export interface PlanVsActualRow {
  step: PlannedStep;
  /** True once fan-out expansion has produced concrete children at execution time (§4.5). */
  isFanoutTemplate: boolean;
  /** Set when a step ID matches an actual execution step exactly. */
  actual: RunStepSummary | null;
  /** Set for a fan-out template whose expanded children (`step[0]`, `step[1]`, …) actually ran. */
  fanoutChildren: RunStepSummary[];
  ran: boolean;
}

export interface PlanVsActual {
  rows: PlanVsActualRow[];
  /** Actual steps that ran but don't map to any step in the *current* plan
   *  snapshot — most commonly execution from before a replan replaced the
   *  plan `RunResource.plan` now reflects. Never dropped silently. */
  unplanned: RunStepSummary[];
}

/**
 * Joins the planned step list against the run's actual execution steps.
 * Never derives "did this run" from plan state alone (`PlanStep.status` is
 * a planning-time/fan-out bookkeeping field, not a live execution mirror —
 * see AGENT-004's fan-out re-entry guard) and never invents an execution
 * result for a step with no matching actual entry.
 */
export function buildPlanVsActual(
  plannedSteps: PlannedStep[],
  actualSteps: RunStepSummary[]
): PlanVsActual {
  const actualById = new Map(actualSteps.map((s) => [s.step_id, s]));
  const consumed = new Set<string>();

  const rows: PlanVsActualRow[] = plannedSteps.map((step) => {
    const actual = actualById.get(step.step_id) ?? null;
    if (actual) consumed.add(step.step_id);

    let fanoutChildren: RunStepSummary[] = [];
    if (!actual && step.fanout) {
      const prefix = `${step.step_id}[`;
      fanoutChildren = actualSteps.filter((s) => s.step_id.startsWith(prefix));
      for (const child of fanoutChildren) consumed.add(child.step_id);
    }

    return {
      step,
      isFanoutTemplate: step.fanout !== null,
      actual,
      fanoutChildren,
      ran: actual !== null || fanoutChildren.length > 0,
    };
  });

  const unplanned = actualSteps.filter((s) => !consumed.has(s.step_id));

  return { rows, unplanned };
}
