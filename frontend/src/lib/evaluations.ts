/**
 * Small, pure render helpers for the evaluations UI (FE-005). Every value
 * here comes straight from the API response — nothing recomputes a
 * pass/fail verdict, an invariant, or a metric the backend already decided
 * (architecture.md §15).
 */
import type { EvaluationResultResource } from "@/lib/api/client";

/** One entry of `EvaluationResultResource.assertions` — a loosely typed
 * JSON blob server-side (`dict[str, Any]`), since a case assertion and a
 * §15.6 invariant violation share the array but carry different optional
 * fields (`invariant`, `evidence`). Parsed defensively, never assumed. */
export interface ParsedAssertion {
  name: string;
  passed: boolean;
  detail: string;
  invariant: string | null;
  evidence: Record<string, unknown> | null;
}

export function parseAssertions(
  raw: EvaluationResultResource["assertions"] | undefined
): ParsedAssertion[] {
  return (raw ?? [])
    .filter((entry): entry is Record<string, unknown> => typeof entry === "object" && entry !== null)
    .map((entry) => ({
      name: typeof entry.name === "string" ? entry.name : "assertion",
      passed: entry.passed === true,
      detail: typeof entry.detail === "string" ? entry.detail : "",
      invariant: typeof entry.invariant === "string" ? entry.invariant : null,
      evidence:
        typeof entry.evidence === "object" && entry.evidence !== null
          ? (entry.evidence as Record<string, unknown>)
          : null,
    }));
}

/** `evaluation_runs.metrics` (§15.4) — a JSONB snapshot, so every field is
 * optional here even though the runner always writes the same shape today. */
export interface EvaluationMetricsSnapshot {
  case_pass_rate?: number;
  task_success_rate?: number;
  avg_duration_ms?: number;
  p50_duration_ms?: number;
  p95_duration_ms?: number;
  total_cases?: number;
  passed_cases?: number;
  failed_cases?: number;
  total_retries?: number;
  avg_retries_per_run?: number;
  approval_wait_ms?: number;
  failure_mix?: Record<string, number>;
}

export function asMetricsSnapshot(metrics: Record<string, unknown>): EvaluationMetricsSnapshot {
  return metrics as EvaluationMetricsSnapshot;
}

/** `0.8234` -> `"82%"`. Returns "—" for a missing/invalid rate rather than a fabricated 0%. */
export function formatRate(rate: number | null | undefined): string {
  if (rate == null || Number.isNaN(rate)) return "—";
  return `${Math.round(rate * 100)}%`;
}

export function formatCount(value: number | null | undefined): string {
  return value == null ? "—" : String(value);
}

/** The three suites `evals/suites.yaml` is required to declare (architecture.md §15.3). */
export const REQUIRED_SUITE_NAMES = ["all", "smoke", "safety"] as const;
