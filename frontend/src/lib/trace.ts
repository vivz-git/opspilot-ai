import type { TraceEventResource } from "@/lib/api/client";

const TOOL_KINDS = new Set([
  "tool_started",
  "tool_succeeded",
  "tool_failed",
  "tool_timeout",
  "tool_duplicate_suppressed",
]);
const VERIFICATION_KINDS = new Set(["verification_passed", "verification_failed", "verification_skipped"]);
const APPROVAL_RESOLUTION_KINDS = new Set([
  "approval_granted",
  "approval_rejected",
  "approval_expired",
  "approval_superseded",
]);
/** Emitted around every node call (§14.4) — too dense to be useful as its own timeline row; the
 *  tool/verification/approval/retry events they wrap already carry the signal an operator needs. */
const HIDDEN_KINDS = new Set(["node_entered", "node_exited"]);

function payloadString(payload: TraceEventResource["payload"], key: string): string | null {
  const value = (payload as Record<string, unknown> | undefined)?.[key];
  return typeof value === "string" ? value : null;
}

export interface AttemptBlock {
  kind: "attempt";
  seq: number;
  attempt: number;
  started: TraceEventResource | null;
  closed: TraceEventResource | null;
  verifications: TraceEventResource[];
}

export interface EventBlock {
  kind: "event";
  seq: number;
  event: TraceEventResource;
}

export type StepBlock = AttemptBlock | EventBlock;

/**
 * Groups one step's events into per-attempt blocks (input/output/error/
 * verification) plus any other step-scoped events (e.g. `retry_scheduled`),
 * ordered by `seq` — the documented ordering key (§14.3), not client
 * inference. A `retry_scheduled` marker is never number-matched to an
 * attempt: its `attempt` field is the *retry count*, a different unit from
 * the tool `attempt` counter, so seq position (which is causally correct
 * by construction) is the only honest way to place it.
 */
export function groupStepBlocks(stepEvents: TraceEventResource[]): StepBlock[] {
  const sorted = [...stepEvents].sort((a, b) => a.seq - b.seq);
  const attempts = new Map<number, AttemptBlock>();
  const blocks: StepBlock[] = [];

  for (const event of sorted) {
    if ((TOOL_KINDS.has(event.kind) || VERIFICATION_KINDS.has(event.kind)) && event.attempt != null) {
      let block = attempts.get(event.attempt);
      if (!block) {
        block = { kind: "attempt", seq: event.seq, attempt: event.attempt, started: null, closed: null, verifications: [] };
        attempts.set(event.attempt, block);
        blocks.push(block);
      }
      if (event.kind === "tool_started") block.started = event;
      else if (TOOL_KINDS.has(event.kind)) block.closed = event;
      else block.verifications.push(event);
      continue;
    }
    blocks.push({ kind: "event", seq: event.seq, event });
  }

  return blocks.sort((a, b) => blockSeq(a) - blockSeq(b));
}

function blockSeq(block: StepBlock): number {
  if (block.kind === "event") return block.seq;
  const seqs = [block.started?.seq, block.closed?.seq, ...block.verifications.map((v) => v.seq)].filter(
    (s): s is number => s != null
  );
  return seqs.length > 0 ? Math.min(...seqs) : block.seq;
}

export interface StepGroupEntry {
  kind: "step";
  seq: number;
  stepId: string;
  tool: string | null;
  blocks: StepBlock[];
}

export interface ApprovalGapEntry {
  kind: "approval-gap";
  seq: number;
  approvalId: string;
  stepId: string | null;
  requested: TraceEventResource;
  resolved: TraceEventResource | null;
  waitMs: number | null;
}

export interface MarkerEntry {
  kind: "marker";
  seq: number;
  event: TraceEventResource;
}

export type TimelineEntry = StepGroupEntry | ApprovalGapEntry | MarkerEntry;

/**
 * Builds the chronological run timeline from the persisted trace (§14.7 #2):
 * step-scoped events (tool attempts, retries, verification) nest under one
 * entry per step; an `approval_requested` event opens a labelled gap that
 * closes at its matching decision event (matched by `payload.approval_id`,
 * the field the backend actually stamps on every approval-kind event); the
 * pause itself has no events because none are emitted while genuinely
 * paused (§9: `request_approval` calls `interrupt()` before any side
 * effect) — the gap *is* the absence, rendered explicitly rather than left
 * as a silent hole in the list.
 */
export function buildTimeline(events: TraceEventResource[]): TimelineEntry[] {
  const sorted = [...events].sort((a, b) => a.seq - b.seq);

  const resolutionByApprovalId = new Map<string, TraceEventResource>();
  for (const event of sorted) {
    if (APPROVAL_RESOLUTION_KINDS.has(event.kind)) {
      const id = payloadString(event.payload, "approval_id");
      if (id) resolutionByApprovalId.set(id, event);
    }
  }
  const consumedResolutionSeqs = new Set<number>();

  const stepGroups = new Map<string, StepGroupEntry>();
  const stepEventsById = new Map<string, TraceEventResource[]>();
  const entries: TimelineEntry[] = [];

  for (const event of sorted) {
    if (HIDDEN_KINDS.has(event.kind)) continue;

    if (event.kind === "approval_requested") {
      const approvalId = payloadString(event.payload, "approval_id") ?? "";
      const resolved = approvalId ? (resolutionByApprovalId.get(approvalId) ?? null) : null;
      if (resolved) consumedResolutionSeqs.add(resolved.seq);
      entries.push({
        kind: "approval-gap",
        seq: event.seq,
        approvalId,
        stepId: event.step_id ?? null,
        requested: event,
        resolved,
        waitMs: resolved ? new Date(resolved.ts).getTime() - new Date(event.ts).getTime() : null,
      });
      continue;
    }
    if (APPROVAL_RESOLUTION_KINDS.has(event.kind) && consumedResolutionSeqs.has(event.seq)) {
      continue; // rendered inline as part of its approval-gap entry above
    }

    if (event.step_id) {
      const list = stepEventsById.get(event.step_id) ?? [];
      list.push(event);
      stepEventsById.set(event.step_id, list);

      let group = stepGroups.get(event.step_id);
      if (!group) {
        group = {
          kind: "step",
          seq: event.seq,
          stepId: event.step_id,
          tool: event.tool ?? null,
          blocks: [],
        };
        stepGroups.set(event.step_id, group);
        entries.push(group);
      }
      continue;
    }

    entries.push({ kind: "marker", seq: event.seq, event });
  }

  for (const group of stepGroups.values()) {
    group.blocks = groupStepBlocks(stepEventsById.get(group.stepId) ?? []);
  }

  return entries.sort((a, b) => a.seq - b.seq);
}

/** All events for one step, in seq order — the input to the Step Inspector. */
export function eventsForStep(events: TraceEventResource[], stepId: string): TraceEventResource[] {
  return events.filter((e) => e.step_id === stepId).sort((a, b) => a.seq - b.seq);
}
