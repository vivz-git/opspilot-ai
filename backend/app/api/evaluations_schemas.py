"""Pydantic v2 schemas for the evaluation HTTP API (§13.7, §15).

Safe external representations of `evaluation_runs`/`evaluation_results`
(§12.8) — the same rows `EvaluationRunner` (EVAL-002) already writes. This
module renders them; it computes nothing of its own.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.state import PlannerKind
from app.persistence.models import EvaluationResult, EvaluationRun, EvaluationRunStatus

__all__ = [
    "EvaluationMetricsResponse",
    "EvaluationRunCreateRequest",
    "EvaluationRunListResponse",
    "EvaluationRunResource",
    "EvaluationResultResource",
]


class EvaluationRunCreateRequest(BaseModel):
    """Payload for POST /evaluations/runs."""

    model_config = ConfigDict(extra="forbid")

    suite: str = Field(
        default="all",
        min_length=1,
        max_length=64,
        description="Suite name declared in evals/suites.yaml.",
    )


class EvaluationRunResource(BaseModel):
    """One `evaluation_runs` row (§12.8)."""

    model_config = ConfigDict(extra="forbid")

    evaluation_run_id: uuid.UUID
    suite: str
    status: EvaluationRunStatus
    started_at: datetime
    finished_at: datetime | None = None
    planner_kind: PlannerKind
    git_sha: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    seed: int | None = None
    case_count: int
    passed: int
    failed: int
    metrics: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: EvaluationRun) -> EvaluationRunResource:
        return cls(
            evaluation_run_id=row.id,
            suite=row.suite,
            status=row.status,
            started_at=row.started_at,
            finished_at=row.finished_at,
            planner_kind=row.planner_kind,
            git_sha=row.git_sha,
            model_id=row.model_id,
            prompt_version=row.prompt_version,
            seed=row.seed,
            case_count=row.case_count,
            passed=row.passed,
            failed=row.failed,
            metrics=row.metrics or {},
        )


class EvaluationRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[EvaluationRunResource]


class EvaluationResultResource(BaseModel):
    """One `evaluation_results` row (§12.8) — a single case's outcome."""

    model_config = ConfigDict(extra="forbid")

    result_id: uuid.UUID
    evaluation_run_id: uuid.UUID
    case_id: str
    run_id: uuid.UUID
    passed: bool
    assertions: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int | None = None
    retry_count: int
    tool_calls_count: int
    approval_outcome: str | None = None
    failure_reason: str | None = None

    @classmethod
    def from_row(cls, row: EvaluationResult) -> EvaluationResultResource:
        return cls(
            result_id=row.id,
            evaluation_run_id=row.evaluation_run_id,
            case_id=row.case_id,
            run_id=row.run_id,
            passed=row.passed,
            assertions=list(row.assertions or []),
            duration_ms=row.duration_ms,
            retry_count=row.retry_count,
            tool_calls_count=row.tool_calls_count,
            approval_outcome=row.approval_outcome,
            failure_reason=row.failure_reason,
        )


class EvaluationMetricsResponse(BaseModel):
    """GET /evaluations/metrics — the §15.4 snapshot of the latest matching run.

    Not a new computation: `metrics` is copied verbatim from the most recent
    `evaluation_runs.metrics` (optionally filtered by `suite`), the same
    JSONB the runner (EVAL-003) already wrote.
    """

    model_config = ConfigDict(extra="forbid")

    evaluation_run_id: uuid.UUID | None = None
    suite: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
