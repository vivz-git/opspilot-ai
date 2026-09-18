"""Evaluation metrics computation layer (§15.4, EVAL-003).

Computes deterministic metric snapshots for evaluation runs:
- `case_pass_rate`: passed cases / total cases (regression gate)
- `task_success_rate`: completed runs / runs whose case expects completion
  (excludes cases expecting rejection or failure from denominator)
- `agent_duration_ms`: duration_ms - approval_wait_ms (excludes human wait time)
- Duration percentiles (`avg_duration_ms`, `p50_duration_ms`, `p95_duration_ms`)
- Retry and failure mix breakdowns
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from app.agent.state import ApprovalStatus, RunStatus
from app.persistence.models import AgentRun, ApprovalRow, TraceEvent

if TYPE_CHECKING:
    from app.evaluation.runner import CaseResult
    from app.evaluation.schemas import EvalCase

__all__ = [
    "CaseMetricInput",
    "SuiteRunResult",
    "calculate_agent_duration_ms",
    "calculate_approval_wait_ms",
    "calculate_case_pass_rate",
    "calculate_task_success_rate",
    "compute_evaluation_metrics",
    "compute_evaluation_metrics_from_results",
]

_APPROVAL_REQ_EVENT: Final = "approval_requested"
_APPROVAL_RESOLVE_EVENTS: Final[frozenset[str]] = frozenset(
    {"approval_granted", "approval_rejected", "approval_expired", "approval_superseded"}
)


@dataclass(frozen=True)
class CaseMetricInput:
    """Input evidence for a single case metric calculation."""

    case_id: str
    passed: bool
    expected_final_status: RunStatus
    actual_final_status: RunStatus
    duration_ms: int
    agent_duration_ms: int
    approval_wait_ms: int = 0
    tool_calls_count: int = 0
    retry_count: int = 0
    status_reason: str | None = None


def calculate_approval_wait_ms(
    approvals: Sequence[ApprovalRow] | None = None,
    events: Sequence[TraceEvent] | None = None,
    run: AgentRun | None = None,
) -> int:
    """Calculate total human approval waiting time in milliseconds (§15.4).

    Reuses existing persistence evidence:
    - Primary: `ApprovalRow.requested_at` and `decided_at`
      (or `expires_at` / `run.finished_at`).
    - Trace fallback: `TraceEventKind.APPROVAL_REQUESTED` paired with
      `APPROVAL_GRANTED` / `APPROVAL_REJECTED` / `APPROVAL_EXPIRED` / `APPROVAL_SUPERSEDED`.
    """
    total_wait_ms = 0

    if approvals:
        for a in approvals:
            if a.decided_at is not None:
                delta = (a.decided_at - a.requested_at).total_seconds()
                total_wait_ms += max(0, int(delta * 1000))
            elif a.status is ApprovalStatus.EXPIRED and a.expires_at is not None:
                delta = (a.expires_at - a.requested_at).total_seconds()
                total_wait_ms += max(0, int(delta * 1000))
            elif (
                a.status is ApprovalStatus.PENDING
                and run is not None
                and run.finished_at is not None
            ):
                delta = (run.finished_at - a.requested_at).total_seconds()
                total_wait_ms += max(0, int(delta * 1000))
        return total_wait_ms

    if events:
        req_times: dict[str, datetime] = {}
        for e in events:
            approval_id = str(e.payload.get("approval_id") or e.step_id or "default")
            kind_val = getattr(e.kind, "value", str(e.kind))
            if kind_val == _APPROVAL_REQ_EVENT:
                req_times[approval_id] = e.ts
            elif kind_val in _APPROVAL_RESOLVE_EVENTS and approval_id in req_times:
                req_ts = req_times.pop(approval_id)
                delta = (e.ts - req_ts).total_seconds()
                total_wait_ms += max(0, int(delta * 1000))

        if run is not None and run.finished_at is not None:
            for req_ts in req_times.values():
                delta = (run.finished_at - req_ts).total_seconds()
                total_wait_ms += max(0, int(delta * 1000))

        return total_wait_ms

    return 0


def calculate_agent_duration_ms(run_duration_ms: int | None, approval_wait_ms: int) -> int:
    """agent_duration_ms = run duration_ms - approval_wait_ms (§15.4).

    Human approval waiting time is excluded.
    """
    if run_duration_ms is None:
        return 0
    return max(0, run_duration_ms - max(0, approval_wait_ms))


def calculate_case_pass_rate(passed_cases: int, total_cases: int) -> float:
    """case_pass_rate = passed cases / total cases (§15.4).

    The regression gate. A case expecting rejection and getting it is a pass.
    """
    if total_cases <= 0:
        return 0.0
    return passed_cases / total_cases


def calculate_task_success_rate(completed_runs: int, total_completion_expected_cases: int) -> float:
    """task_success_rate = runs reaching completed / runs whose case expects completion (§15.4).

    Deliberately excludes cases that should end rejected or failed from denominator.
    """
    if total_completion_expected_cases <= 0:
        return 0.0
    return completed_runs / total_completion_expected_cases


def _percentile(sorted_data: Sequence[float | int], percentile: float) -> float:
    """Calculate the p-th percentile from a sorted sequence using linear interpolation."""
    if not sorted_data:
        return 0.0
    if len(sorted_data) == 1:
        return float(sorted_data[0])
    k = (len(sorted_data) - 1) * (percentile / 100.0)
    f = int(math.floor(k))
    c = int(math.ceil(k))
    if f == c:
        return float(sorted_data[f])
    d0 = float(sorted_data[f]) * (c - k)
    d1 = float(sorted_data[c]) * (k - f)
    return d0 + d1


def compute_evaluation_metrics(case_inputs: Sequence[CaseMetricInput]) -> dict[str, Any]:
    """Compute the §15.4 metrics snapshot across case inputs."""
    total_cases = len(case_inputs)
    passed_cases = sum(1 for c in case_inputs if c.passed)
    failed_cases = total_cases - passed_cases

    case_pass_rate = calculate_case_pass_rate(passed_cases, total_cases)

    completion_expected = [c for c in case_inputs if c.expected_final_status is RunStatus.COMPLETED]
    completed_runs = sum(
        1 for c in completion_expected if c.actual_final_status is RunStatus.COMPLETED
    )
    task_success_rate = calculate_task_success_rate(completed_runs, len(completion_expected))

    agent_durations = [c.agent_duration_ms for c in case_inputs]
    total_wait_ms = sum(c.approval_wait_ms for c in case_inputs)

    if agent_durations:
        sorted_durations = sorted(agent_durations)
        avg_dur = round(sum(agent_durations) / len(agent_durations), 2)
        p50 = round(_percentile(sorted_durations, 50), 2)
        p95 = round(_percentile(sorted_durations, 95), 2)
    else:
        avg_dur = 0.0
        p50 = 0.0
        p95 = 0.0

    total_retries = sum(c.retry_count for c in case_inputs)
    failed_run_count = sum(1 for c in case_inputs if c.actual_final_status is RunStatus.FAILED)
    failure_mix: dict[str, int] = {}
    for c in case_inputs:
        if (
            c.actual_final_status not in (RunStatus.COMPLETED, RunStatus.RUNNING)
            and c.status_reason
        ):
            failure_mix[c.status_reason] = failure_mix.get(c.status_reason, 0) + 1

    return {
        "case_pass_rate": case_pass_rate,
        "task_success_rate": task_success_rate,
        "agent_duration_ms": avg_dur,
        "avg_duration_ms": avg_dur,
        "p50_duration_ms": p50,
        "p95_duration_ms": p95,
        "p50": p50,
        "p95": p95,
        "total_cases": total_cases,
        "passed_cases": passed_cases,
        "failed_cases": failed_cases,
        "completion_expected_cases": len(completion_expected),
        "completed_runs": completed_runs,
        "approval_wait_ms": total_wait_ms,
        "total_retries": total_retries,
        "avg_retries_per_run": round(total_retries / total_cases, 2) if total_cases > 0 else 0.0,
        "failed_runs": failed_run_count,
        "failure_mix": failure_mix,
    }


def compute_evaluation_metrics_from_results(
    cases: Sequence[EvalCase],
    results: Sequence[CaseResult],
) -> dict[str, Any]:
    """Compute suite metrics from EvalCase specifications and CaseResult outcomes."""
    case_map = {c.id: c for c in cases}
    inputs: list[CaseMetricInput] = []
    for r in results:
        case = case_map.get(r.case_id)
        expected_status = case.expect.final_status if case is not None else RunStatus.COMPLETED
        inputs.append(
            CaseMetricInput(
                case_id=r.case_id,
                passed=r.passed,
                expected_final_status=expected_status,
                actual_final_status=r.final_status,
                duration_ms=r.duration_ms,
                agent_duration_ms=r.agent_duration_ms,
                approval_wait_ms=r.approval_wait_ms,
                tool_calls_count=r.tool_calls_count,
                retry_count=r.retry_count,
                status_reason=r.status_reason,
            )
        )
    return compute_evaluation_metrics(inputs)


class SuiteRunResult(list["CaseResult"]):
    """Result of an evaluation suite execution.

    Inherits from `list[CaseResult]` to remain transparently compatible with all
    existing callers and test assertions, while exposing `.evaluation_run_id`
    and `.metrics` (§15.4).
    """

    def __init__(
        self,
        results: Sequence[CaseResult],
        *,
        evaluation_run_id: uuid.UUID | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(results)
        self.evaluation_run_id = evaluation_run_id
        self.metrics = metrics or {}
