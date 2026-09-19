"""Evaluation HTTP control-plane endpoints (§13.6, API-005).

Exposes:
- `POST /evaluations/runs`            trigger the real suite mechanism (§15.5)
- `GET  /evaluations/runs`            list suite runs, newest first
- `GET  /evaluations/runs/{id}`       a single suite run and its metrics
- `GET  /evaluations/runs/{id}/results`  per-case results, `?passed=false`
- `GET  /evaluations/metrics`         the persisted metric snapshot per run

Delegates everything to `EvaluationService`, which schedules the existing
`EvaluationRunner.run_suite` in the background and reads back the
`evaluation_runs`/`evaluation_results` rows it persists — the same rows
`python -m app.evaluation.cli` writes. No second execution path.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_evaluation_service, require_authorization
from app.api.schemas import (
    EvaluationMetricsEntry,
    EvaluationMetricsResponse,
    EvaluationResultListResponse,
    EvaluationResultResource,
    EvaluationRunCreateRequest,
    EvaluationRunListResponse,
    EvaluationRunResource,
)
from app.execution.evaluations import EvaluationService

__all__ = [
    "router",
]

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.post("/runs", response_model=EvaluationRunResource, status_code=status.HTTP_202_ACCEPTED)
async def create_evaluation_run(
    body: EvaluationRunCreateRequest,
    service: EvaluationService = Depends(get_evaluation_service),
    _auth: None = Depends(require_authorization),
) -> EvaluationRunResource:
    """Trigger the existing suite mechanism in the background (§13.6, §15.5)."""
    run = await service.trigger_run(suite=body.suite, case_ids=body.case_ids, planner=body.planner)
    return EvaluationRunResource.from_row(run)


@router.get("/runs", response_model=EvaluationRunListResponse)
async def list_evaluation_runs(
    suite: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=25, ge=1, le=100),
    service: EvaluationService = Depends(get_evaluation_service),
    _auth: None = Depends(require_authorization),
) -> EvaluationRunListResponse:
    """List evaluation suite runs, newest first (§13.6)."""
    runs = await service.list_runs(suite=suite, limit=limit)
    return EvaluationRunListResponse(items=[EvaluationRunResource.from_row(r) for r in runs])


@router.get("/runs/{evaluation_run_id}", response_model=EvaluationRunResource)
async def get_evaluation_run(
    evaluation_run_id: uuid.UUID,
    service: EvaluationService = Depends(get_evaluation_service),
    _auth: None = Depends(require_authorization),
) -> EvaluationRunResource:
    """Retrieve one evaluation suite run and its metrics snapshot (§13.6)."""
    run = await service.require_run(evaluation_run_id)
    return EvaluationRunResource.from_row(run)


@router.get("/runs/{evaluation_run_id}/results", response_model=EvaluationResultListResponse)
async def list_evaluation_results(
    evaluation_run_id: uuid.UUID,
    passed: bool | None = Query(default=None, description="Filter to passed=false for failures."),
    service: EvaluationService = Depends(get_evaluation_service),
    _auth: None = Depends(require_authorization),
) -> EvaluationResultListResponse:
    """Per-case results for a suite run; each carries the real `run_id` (§13.6)."""
    results = await service.list_results(evaluation_run_id, passed=passed)
    return EvaluationResultListResponse(
        items=[EvaluationResultResource.from_row(r) for r in results]
    )


@router.get("/metrics", response_model=EvaluationMetricsResponse)
async def get_evaluation_metrics(
    window: str | None = Query(default=None, description="e.g. '30d' or '24h'."),
    suite: str | None = Query(default=None, max_length=64),
    service: EvaluationService = Depends(get_evaluation_service),
    _auth: None = Depends(require_authorization),
) -> EvaluationMetricsResponse:
    """Metric time series for the dashboard, read from persisted suite runs (§13.6, §15.4)."""
    runs = await service.get_metrics(suite=suite, window=window)
    return EvaluationMetricsResponse(
        window=window,
        suite=suite,
        items=[EvaluationMetricsEntry.from_row(r) for r in runs],
    )
